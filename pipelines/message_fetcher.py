"""消息抓取(白/黑名单 + 单聊条数过滤)。

用 ``ctx.message.get_by_time`` / ``ctx.message.get_by_time_in_chat`` 替代旧
``message_api.*``。host data.py 接受 str 或 number 的 start/end_time(内部
``float(...)``),所以传 str 安全。

返回的消息是 dict 列表(host 序列化后),字段参考
``src/plugin_runtime/host/message_utils.py:_session_message_to_dict``。
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from ._envelope import peel_envelope
from .chat_resolver import ChatResolver, parse_target_config, resolve_filter_strategy

if TYPE_CHECKING:
    from maibot_sdk import PluginContext

logger = logging.getLogger(__name__)


def _msg_time(msg: Dict[str, Any]) -> float:
    """统一从 dict 消息取 unix 时间戳(host 给的是 str)。"""
    raw = msg.get("timestamp", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _msg_session_id(msg: Dict[str, Any]) -> str:
    return str(msg.get("session_id", "") or "")


def _msg_user_id(msg: Dict[str, Any]) -> str:
    info = msg.get("message_info") or {}
    user_info = info.get("user_info") or {}
    return str(user_info.get("user_id", "") or "")


def _msg_group_id(msg: Dict[str, Any]) -> str:
    info = msg.get("message_info") or {}
    group_info = info.get("group_info") or {}
    return str(group_info.get("group_id", "") or "")


class MessageFetcher:
    """按过滤模式拉消息。"""

    def __init__(self, ctx: "PluginContext", chat_resolver: ChatResolver) -> None:
        self._ctx = ctx
        self._resolver = chat_resolver

    async def _list_messages(
        self,
        start_time: float,
        end_time: float,
        chat_id: str = "",
    ) -> List[Dict[str, Any]]:
        """调 ctx.message,统一参数 + 剥信封 + 取 messages 字段。"""
        kwargs: Dict[str, Any] = {
            "start_time": str(start_time),
            "end_time": str(end_time),
            "limit": 0,
            "limit_mode": "earliest",
            "filter_mai": False,
            "filter_command": False,
        }
        try:
            if chat_id:
                result = await self._ctx.message.get_by_time_in_chat(chat_id, **kwargs)
            else:
                # get_by_time 不接受 filter_command(host 实现见 data.py:340)
                kwargs.pop("filter_command", None)
                result = await self._ctx.message.get_by_time(**kwargs)
        except Exception as exc:
            logger.error("ctx.message 查询失败 (chat_id=%s): %s", chat_id, exc, exc_info=True)
            return []

        # SDK _normalize_capability_result 已自动剥 message.get_by_time* 的 messages 字段,
        # 正常时直接返回 list[dict]。但 host 返回 success=False / 走 unknown capability 时
        # 会保留 dict envelope,这里两种形态都兼容。
        result = peel_envelope(result)
        if isinstance(result, list):
            return [m for m in result if isinstance(m, dict)]
        if isinstance(result, dict):
            if not result.get("success", True):
                logger.warning("ctx.message 返回 success=False: %s", result.get("error"))
                return []
            messages = result.get("messages") or []
            return [m for m in messages if isinstance(m, dict)]
        logger.warning("ctx.message 返回非 list/dict: %s", type(result).__name__)
        return []

    async def fetch_for_chats(
        self,
        chat_ids: List[str],
        start_time: float,
        end_time: float,
    ) -> List[Dict[str, Any]]:
        """对每个 session_id 单独取消息,合并并按时间排序。"""
        all_msgs: List[Dict[str, Any]] = []
        for chat_id in chat_ids:
            if not chat_id:
                continue
            msgs = await self._list_messages(start_time, end_time, chat_id=chat_id)
            all_msgs.extend(msgs)
            logger.debug("chat_id=%s 取得 %d 条消息", chat_id, len(msgs))
        all_msgs.sort(key=_msg_time)
        return all_msgs

    async def fetch_all(self, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """全局拉消息(不指定 chat_id)。"""
        msgs = await self._list_messages(start_time, end_time, chat_id="")
        msgs.sort(key=_msg_time)
        return msgs

    async def fetch_with_filter(
        self,
        filter_mode: str,
        target_chats: List[str],
        start_time: float,
        end_time: float,
    ) -> List[Dict[str, Any]]:
        """根据 filter_mode 决定取哪些消息。"""
        strategy, configs = resolve_filter_strategy(filter_mode, target_chats)

        if strategy == "DISABLE_SCHEDULER":
            logger.info("[fetch] 白名单为空,跳过抓取")
            return []
        if strategy == "PROCESS_WHITELIST":
            session_ids = await self._resolver.resolve_to_session_ids(configs)
            if not session_ids:
                logger.warning("白名单解析后为空,无消息可拉")
                return []
            return await self.fetch_for_chats(session_ids, start_time, end_time)
        if strategy == "PROCESS_ALL":
            return await self.fetch_all(start_time, end_time)
        if strategy == "PROCESS_BLACKLIST":
            all_msgs = await self.fetch_all(start_time, end_time)
            return self._filter_blacklist(all_msgs, configs)
        logger.warning("未知 strategy=%s,返回空", strategy)
        return []

    @staticmethod
    def _filter_blacklist(
        messages: List[Dict[str, Any]],
        excluded_configs: List[str],
    ) -> List[Dict[str, Any]]:
        """从 messages 中剔除黑名单聊天的消息。"""
        if not excluded_configs:
            return messages
        excluded_groups, excluded_privates = parse_target_config(excluded_configs)
        excluded_group_set = set(excluded_groups)
        excluded_private_set = set(excluded_privates)

        filtered: List[Dict[str, Any]] = []
        for msg in messages:
            user_id = _msg_user_id(msg)
            group_id = _msg_group_id(msg)
            if group_id and group_id in excluded_group_set:
                continue
            # 私聊判定:无 group_id 视为私聊
            if not group_id and user_id and user_id in excluded_private_set:
                continue
            filtered.append(msg)
        return filtered

    @staticmethod
    def filter_min_messages_per_chat(
        messages: List[Dict[str, Any]],
        min_per_chat: int,
    ) -> List[Dict[str, Any]]:
        """剔除消息数 < min_per_chat 的聊天。"""
        if min_per_chat <= 0 or not messages:
            return messages
        by_chat: Dict[str, List[Dict[str, Any]]] = {}
        for msg in messages:
            sid = _msg_session_id(msg)
            by_chat.setdefault(sid, []).append(msg)
        filtered: List[Dict[str, Any]] = []
        for sid, msgs in by_chat.items():
            if len(msgs) >= min_per_chat:
                filtered.extend(msgs)
                logger.debug("[filter] 保留 %s: %d 条", sid, len(msgs))
            else:
                logger.debug("[filter] 过滤 %s: %d 条 < %d", sid, len(msgs), min_per_chat)
        filtered.sort(key=_msg_time)
        return filtered
