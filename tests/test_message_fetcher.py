"""消息分段必须取全、保持会话范围，并把失败与零消息区分开。"""

import asyncio
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def fetcher_module():
    # Runner 为插件创建独立包；这里只加载消息模块，不导入无关的模型/发布模块。
    namespace = "_diary_message_tests"
    root = Path(__file__).resolve().parents[1]
    for name, path in ((namespace, root), (f"{namespace}.pipelines", root / "pipelines")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    try:
        yield importlib.import_module(f"{namespace}.pipelines.message_fetcher")
    finally:
        for name in list(sys.modules):
            if name == namespace or name.startswith(f"{namespace}."):
                del sys.modules[name]


BASE = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()


def message(index, timestamp, session="chat_a", text="聊天内容"):
    return {
        "message_id": str(index),
        "timestamp": str(timestamp),
        "session_id": session,
        "processed_plain_text": text,
    }


class MessageHost:
    """保留宿主的闭区间、微秒精度、会话过滤及字节帧限制语义。"""

    def __init__(self, messages, *, max_frame_bytes=None, failure=None, envelope=False):
        self.messages = sorted(messages, key=lambda item: float(item["timestamp"]))
        self.max_frame_bytes = max_frame_bytes
        self.failure = failure
        self.envelope = envelope

    async def get_by_time(self, start_time, end_time, **kwargs):
        return self.query(start_time, end_time, kwargs)

    async def get_by_time_in_chat(self, chat_id, start_time, end_time, **kwargs):
        return self.query(start_time, end_time, kwargs, chat_id)

    def query(self, start_time, end_time, kwargs, chat_id=None):
        start = datetime.fromtimestamp(float(start_time))
        end = datetime.fromtimestamp(float(end_time))
        if self.failure:
            self.failure(start, end)
        rows = [
            item for item in self.messages
            if start <= datetime.fromtimestamp(float(item["timestamp"])) <= end
            and (chat_id is None or item["session_id"] == chat_id)
        ]
        if kwargs.get("limit", 0):
            rows = rows[:kwargs["limit"]]
        payload = {"success": True, "messages": rows}
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        if self.max_frame_bytes is not None and size > self.max_frame_bytes:
            raise RuntimeError(f"[E_UNKNOWN] 帧大小 {size} 超过最大限制 {self.max_frame_bytes}")
        return payload if self.envelope else rows


def fetcher(module, host):
    return module.MessageFetcher(SimpleNamespace(message=host), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [False, True])
async def test_busy_day_is_complete_after_frame_splitting(fetcher_module, envelope):
    expected = [message(index, BASE + index * 30, text="聊天记录" * 20) for index in range(2600)]
    host = MessageHost(expected, max_frame_bytes=24_000, envelope=envelope)
    actual = await fetcher(fetcher_module, host).fetch_all(BASE, BASE + 86400 - 0.000001)
    assert actual == expected


@pytest.mark.asyncio
async def test_more_than_one_page_at_same_timestamp_is_not_skipped(fetcher_module):
    expected = [message(index, BASE + 5000) for index in range(1010)]
    expected.append(message(1010, BASE + 6000))
    actual = await fetcher(fetcher_module, MessageHost(expected)).fetch_all(BASE, BASE + 7000)
    assert actual == expected


@pytest.mark.asyncio
async def test_microsecond_window_boundaries_are_included_once(fetcher_module):
    expected = [
        message(index, timestamp) for index, timestamp in enumerate(
            (BASE, BASE + 0.123456, BASE + 3600 - 0.000001, BASE + 3600, BASE + 7200)
        )
    ]
    outside = [message("before", BASE - 0.000001), message("after", BASE + 7200 + 0.000001)]
    actual = await fetcher(fetcher_module, MessageHost(outside + expected)).fetch_all(BASE, BASE + 7200)
    assert actual == expected


@pytest.mark.asyncio
async def test_same_message_id_in_different_chats_is_preserved(fetcher_module):
    expected = [message("42", BASE, "chat_a"), message("42", BASE, "chat_b")]
    actual = await fetcher(fetcher_module, MessageHost(expected)).fetch_all(BASE, BASE + 1)
    assert actual == expected


@pytest.mark.asyncio
async def test_selected_chat_remains_scoped_during_splitting(fetcher_module):
    expected = [message(index, BASE + index * 10, "chat_b", "文字" * 80) for index in range(50)]
    other = [message(index, BASE + index * 10, "chat_a") for index in range(50)]
    host = MessageHost(other + expected, max_frame_bytes=2000)
    actual = await fetcher(fetcher_module, host).fetch_for_chats(["chat_b"], BASE, BASE + 3600)
    assert actual == expected


@pytest.mark.asyncio
async def test_failure_after_successful_window_does_not_return_partial_diary(fetcher_module):
    def fail_second_window(start, end):
        if start >= datetime.fromtimestamp(BASE + 3600):
            raise RuntimeError("消息查询权限不足")

    host = MessageHost([message(1, BASE), message(2, BASE + 3600)], failure=fail_second_window)
    with pytest.raises(fetcher_module.MessageFetchError, match="消息查询权限不足"):
        await fetcher(fetcher_module, host).fetch_all(BASE, BASE + 7200)


@pytest.mark.asyncio
async def test_oversized_single_timestamp_fails_instead_of_skipping(fetcher_module):
    rows = [message(1, BASE), message(2, BASE + 3600, text="大消息" * 100)]
    host = MessageHost(rows, max_frame_bytes=400)
    with pytest.raises(fetcher_module.MessageFetchError, match="帧大小"):
        await fetcher(fetcher_module, host).fetch_all(BASE, BASE + 3601)


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_empty_or_retried(fetcher_module):
    def cancel(start, end):
        raise asyncio.CancelledError()

    host = MessageHost([message(1, BASE)], failure=cancel)
    with pytest.raises(asyncio.CancelledError):
        await fetcher(fetcher_module, host).fetch_all(BASE, BASE + 1)


@pytest.mark.asyncio
async def test_genuinely_empty_window_returns_empty(fetcher_module):
    assert await fetcher(fetcher_module, MessageHost([])).fetch_all(BASE, BASE + 1) == []
