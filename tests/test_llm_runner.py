"""pipelines/llm_runner.py 单元测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from maibot_sdk.capabilities.llm import LLMCapability

from diary_plugin.config import DefaultModelSection
from diary_plugin.pipelines.llm_runner import LLMCallError, LLMRunner, _is_rpc_timeout


class TestIsRpcTimeout:
    def test_detects_e_timeout_keyword(self):
        assert _is_rpc_timeout(RuntimeError("[E_TIMEOUT] 请求 cap.call 超时"))

    def test_detects_chinese_phrase(self):
        assert _is_rpc_timeout(Exception("cap.call 超时 (30000ms)"))

    def test_other_errors_not_matched(self):
        assert not _is_rpc_timeout(RuntimeError("normal error"))
        assert not _is_rpc_timeout(ValueError("bad value"))


@pytest.mark.asyncio
async def test_generate_empty_prompt_returns_empty():
    ctx = MagicMock()
    runner = LLMRunner(ctx, DefaultModelSection())
    assert await runner.generate("") == ""
    assert await runner.generate("   ") == ""


@pytest.mark.asyncio
async def test_generate_success():
    ctx = MagicMock()
    ctx.llm.generate = AsyncMock(
        return_value={"success": True, "response": "  这是日记  ", "model": "replyer"}
    )
    runner = LLMRunner(ctx, DefaultModelSection())
    result = await runner.generate("写日记")
    assert result == "这是日记"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_name", ["replyer", "utils", "planner", "vlm"])
async def test_generate_routes_task_through_real_sdk(task_name: str):
    ctx = MagicMock()
    ctx.call_capability = AsyncMock(return_value={"success": True, "response": "diary text"})
    ctx.llm = LLMCapability(ctx)
    config = DefaultModelSection(model_name=task_name, temperature=0.4)

    assert await LLMRunner(ctx, config).generate("write diary") == "diary text"

    ctx.call_capability.assert_awaited_once_with(
        "llm.generate", prompt="write diary", task_name=task_name, model="", temperature=0.4
    )


@pytest.mark.asyncio
async def test_generate_failure_raises():
    ctx = MagicMock()
    ctx.llm.generate = AsyncMock(
        return_value={"success": False, "error": "model unavailable"}
    )
    runner = LLMRunner(ctx, DefaultModelSection())
    with pytest.raises(LLMCallError, match="model unavailable"):
        await runner.generate("写日记")


@pytest.mark.asyncio
async def test_generate_rpc_timeout_gives_clear_hint():
    """E_TIMEOUT 异常应翻译成"建议切 custom_model"提示。"""
    ctx = MagicMock()
    ctx.llm.generate = AsyncMock(
        side_effect=RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (30000ms)")
    )
    runner = LLMRunner(ctx, DefaultModelSection())
    with pytest.raises(LLMCallError) as exc_info:
        await runner.generate("写日记")
    assert "RPC 30s" in str(exc_info.value)
    assert "custom_model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_non_dict_response_raises():
    ctx = MagicMock()
    ctx.llm.generate = AsyncMock(return_value="not a dict")
    runner = LLMRunner(ctx, DefaultModelSection())
    with pytest.raises(LLMCallError, match="非 dict"):
        await runner.generate("写日记")
