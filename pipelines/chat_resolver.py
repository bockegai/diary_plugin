"""聊天 ID 解析器。

把用户配置的 ``group:123`` / ``private:456`` 转成 host 内部的 ``session_id``,
通过 ``ctx.chat.get_stream_by_*`` 查询(替代旧 ORM 直查 ChatStreams)。

仍保留本地缓存(基于配置 hash 失效),减少重复 RPC。
"""

import hashlib
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from ._envelope import peel_envelope

if TYPE_CHECKING:
    from maibot_sdk import PluginContext

logger = logging.getLogger(__name__)

_PRIVATE_TARGET_PLATFORM_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
_PRIVATE_TARGET_ACCOUNT_RE = re.compile(r"^[^\s:]{1,128}$")


def parse_private_target(raw: str) -> Tuple[str, str]:
    """解析 ``平台:qq号``（如 ``qq:111111``）为 ``(platform, account)``。

    同时兼容全角冒号。格式非法时抛 ``ValueError``。
    """
    value = str(raw or "").strip().replace("：", ":", 1)
    if not value:
        raise ValueError("私聊目标为空")
    platform, separator, account = value.partition(":")
    platform = platform.strip().lower()
    account = account.strip()
    if not separator or not platform or not account:
        raise ValueError("私聊目标格式应为 平台:qq号，例如 qq:111111")
    if not _PRIVATE_TARGET_PLATFORM_RE.match(platform):
        raise ValueError(f"私聊目标的平台标识不合法: {platform}")
    if not _PRIVATE_TARGET_ACCOUNT_RE.match(account):
        raise ValueError("私聊目标的账号不能包含空白或冒号")
    return platform, account


def parse_target_config(target_chats: List[str]) -> Tuple[List[str], List[str]]:
    """解析 ``["group:123", "private:456", ...]`` → (groups, privates)。"""
    groups: List[str] = []
    privates: List[str] = []
    for cfg in target_chats:
        if cfg.startswith("group:"):
            groups.append(cfg[6:])
        elif cfg.startswith("private:"):
            privates.append(cfg[8:])
        else:
            logger.warning("无效的聊天配置: %s", cfg)
    return groups, privates


def resolve_filter_strategy(filter_mode: str, target_chats: List[str]) -> Tuple[str, List[str]]:
    """根据过滤模式 + target_chats 计算执行策略。

    Returns:
        ("DISABLE_SCHEDULER" | "PROCESS_WHITELIST" | "PROCESS_BLACKLIST" | "PROCESS_ALL", 配置列表)
    """
    if filter_mode == "whitelist":
        if target_chats:
            return "PROCESS_WHITELIST", target_chats
        return "DISABLE_SCHEDULER", []
    if filter_mode == "blacklist":
        if target_chats:
            return "PROCESS_BLACKLIST", target_chats
        return "PROCESS_ALL", []
    logger.warning("未知 filter_mode=%s,降级为 whitelist 处理", filter_mode)
    return ("PROCESS_WHITELIST" if target_chats else "DISABLE_SCHEDULER", target_chats)


class ChatResolver:
    """ctx.chat 调用 + 本地缓存。"""

    def __init__(self, ctx: "PluginContext", plugin_dir: str = "") -> None:
        self._ctx = ctx
        if plugin_dir:
            base = os.path.abspath(plugin_dir)
        else:
            base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self._cache_file = os.path.join(base, "data", "chat_mapping.json")
        self._cache: dict = {}
        self._last_config_hash = ""
        self._load_cache()

    @staticmethod
    def _config_hash(groups: List[str], privates: List[str]) -> str:
        s = f"groups:{','.join(sorted(groups))};privates:{','.join(sorted(privates))}"
        return hashlib.md5(s.encode()).hexdigest()

    def _load_cache(self) -> None:
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._cache = data.get("mapping", {})
                self._last_config_hash = data.get("config_hash", "")
        except Exception as exc:
            logger.warning("加载缓存失败: %s", exc)

    def _save_cache(self, config_hash: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"mapping": self._cache, "config_hash": config_hash, "last_update": time.time()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            logger.error("保存缓存失败: %s", exc, exc_info=True)

    @staticmethod
    def _extract_session_id(result: Any) -> Optional[str]:
        """从 host 返回值里取 session_id，兼容 stream 包装和直接字段。"""
        result = peel_envelope(result)
        if not isinstance(result, dict):
            return None
        if result.get("success") is False:
            return None
        # 直接 stream 形式或 {success, stream} 形式
        stream = result.get("stream", result if "session_id" in result else None)
        if not stream:
            return None
        if isinstance(stream, dict):
            return stream.get("session_id") or stream.get("stream_id")
        return getattr(stream, "session_id", None) or getattr(stream, "stream_id", None)

    async def _query_session_id(self, qq: str, is_group: bool) -> Optional[str]:
        """调 ``ctx.chat.get_stream_by_*`` 取 session_id。

        host 返回结构(参考 src/plugin_runtime/capabilities/data.py:222):
            {"success": True, "stream": {"session_id": "...", ...}}
        我们用 peel_envelope 兜底剥掉外层信封。
        """
        try:
            if is_group:
                result = await self._ctx.chat.get_stream_by_group_id(qq)
            else:
                result = await self._ctx.chat.get_stream_by_user_id(qq)
        except Exception as exc:
            logger.error("ctx.chat 查询失败 (qq=%s, group=%s): %s", qq, is_group, exc)
            return None

        return self._extract_session_id(result)

    async def resolve_private_stream(self, platform: str, account: str) -> Optional[str]:
        """把 ``平台:账号`` 解析成私聊 session_id。

        优先复用已存在的聊天流；旧版 host 没有 ``chat.open_session`` 时
        只返回已存在的流，不会创建新会话。
        """
        platform = str(platform or "qq").strip().lower() or "qq"
        account = str(account or "").strip()
        if not account:
            return None

        try:
            try:
                result = await self._ctx.chat.get_stream_by_user_id(account, platform=platform)
            except TypeError:
                # 旧版 SDK 的 get_stream_by_user_id 没有 platform 参数。
                result = await self._ctx.chat.get_stream_by_user_id(account)
        except Exception as exc:
            logger.warning("查询私聊流失败 (platform=%s, account=%s): %s", platform, account, exc)
            result = None

        session_id = self._extract_session_id(result)
        if session_id:
            return session_id

        open_session = getattr(self._ctx.chat, "open_session", None)
        if open_session is None:
            logger.warning(
                "未找到私聊 %s:%s 的聊天流，且当前 host 不支持 chat.open_session，无法主动创建",
                platform,
                account,
            )
            return None

        try:
            result = await open_session(platform=platform, chat_type="private", user_id=account)
        except Exception as exc:
            logger.error("创建私聊流失败 (platform=%s, account=%s): %s", platform, account, exc)
            return None

        session_id = self._extract_session_id(result)
        if not session_id:
            logger.warning("创建私聊流后仍未取得 session_id (platform=%s, account=%s)", platform, account)
        return session_id

    async def resolve_to_session_ids(self, target_chats: List[str]) -> List[str]:
        """配置列表 → session_id 列表(去重 + 缓存)。"""
        if not target_chats:
            return []
        groups, privates = parse_target_config(target_chats)
        cur_hash = self._config_hash(groups, privates)
        config_changed = cur_hash != self._last_config_hash

        results: List[str] = []
        for group_qq in groups:
            cache_key = f"group_{group_qq}"
            if not config_changed and cache_key in self._cache:
                results.append(self._cache[cache_key])
                continue
            sid = await self._query_session_id(group_qq, is_group=True)
            if sid:
                self._cache[cache_key] = sid
                results.append(sid)
                logger.debug("群聊映射: %s → %s", group_qq, sid)
            else:
                logger.info("未找到群聊 %s 的 session_id", group_qq)

        for user_qq in privates:
            cache_key = f"private_{user_qq}"
            if not config_changed and cache_key in self._cache:
                results.append(self._cache[cache_key])
                continue
            sid = await self._query_session_id(user_qq, is_group=False)
            if sid:
                self._cache[cache_key] = sid
                results.append(sid)
                logger.debug("私聊映射: %s → %s", user_qq, sid)
            else:
                logger.info("未找到私聊 %s 的 session_id", user_qq)

        if config_changed or results:
            self._save_cache(cur_hash)
            self._last_config_hash = cur_hash
        return results
