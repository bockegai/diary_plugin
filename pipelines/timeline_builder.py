"""聊天时间线构建 + 情感天气 + 图片识别。

输入是 dict 消息列表(host 序列化),字段参考
``src/plugin_runtime/host/message_utils.py:_session_message_to_dict``。

消息为 dict 形式,通过 ``msg.get(...)`` 访问,优先使用 host 提供的 ``is_picture`` 标志。
"""

import datetime
import logging
import random
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _msg_time(msg: Dict[str, Any]) -> float:
    raw = msg.get("timestamp", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _msg_text(msg: Dict[str, Any]) -> str:
    return str(msg.get("processed_plain_text") or "")


def _msg_user_info(msg: Dict[str, Any]) -> Tuple[str, str]:
    """返回 (user_id, nickname)。"""
    info = msg.get("message_info") or {}
    user = info.get("user_info") or {}
    return (
        str(user.get("user_id", "") or ""),
        str(user.get("user_nickname") or "某人"),
    )


def _is_image(msg: Dict[str, Any]) -> bool:
    """优先用 host 提供的 is_picture 标志。"""
    if msg.get("is_picture"):
        return True
    # 兜底:扫 raw_message
    raw = msg.get("raw_message") or []
    if isinstance(raw, list):
        for seg in raw:
            if isinstance(seg, dict) and str(seg.get("type", "")).lower() in ("image", "picture", "img"):
                return True
    return False


def _image_description(msg: Dict[str, Any]) -> str:
    """从 raw_message 中拿图片描述/alt 文本(若有)。"""
    raw = msg.get("raw_message") or []
    if not isinstance(raw, list):
        return ""
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        if str(seg.get("type", "")).lower() not in ("image", "picture", "img"):
            continue
        data = seg.get("data") or {}
        if isinstance(data, dict):
            for key in ("description", "summary", "alt", "file"):
                val = data.get(key)
                if val:
                    return str(val)
    return ""


class TimelineBuilder:
    """聊天时间线构建器。"""

    def __init__(self, bot_qq_account: str = "") -> None:
        self.bot_qq_account = str(bot_qq_account or "")
        self._stats: Dict[str, int] = {"total_messages": 0, "bot_messages": 0, "user_messages": 0}

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def build(self, messages: List[Dict[str, Any]]) -> str:
        """构建时间线文本。同时统计 bot/user 消息数。"""
        if not messages:
            self._stats = {"total_messages": 0, "bot_messages": 0, "user_messages": 0}
            return "今天没有什么特别的对话。"

        parts: List[str] = []
        current_hour = -1
        bot_count = 0
        user_count = 0

        for msg in messages:
            ts = _msg_time(msg)
            try:
                dt = datetime.datetime.fromtimestamp(ts)
            except (OSError, OverflowError, ValueError):
                continue
            hour = dt.hour
            if hour != current_hour:
                if 6 <= hour < 12:
                    period = f"上午{hour}点"
                elif 12 <= hour < 18:
                    period = f"下午{hour}点"
                else:
                    period = f"晚上{hour}点"
                parts.append(f"\n【{period}】")
                current_hour = hour

            user_id, nickname = _msg_user_info(msg)
            is_bot = bool(self.bot_qq_account) and user_id == self.bot_qq_account

            if _is_image(msg):
                desc = _image_description(msg)
                tag = f"[图片]{desc}" if desc else "[图片]"
                if is_bot:
                    parts.append(f"我: {tag}")
                    bot_count += 1
                else:
                    parts.append(f"{nickname}: {tag}")
                    user_count += 1
            else:
                text = _msg_text(msg)
                if text and len(text) > 50:
                    text = text[:50] + "..."
                if is_bot:
                    parts.append(f"我: {text}")
                    bot_count += 1
                else:
                    parts.append(f"{nickname}: {text}")
                    user_count += 1

        self._stats = {
            "total_messages": len(messages),
            "bot_messages": bot_count,
            "user_messages": user_count,
        }
        return "\n".join(parts)


def weather_by_emotion(messages: List[Dict[str, Any]]) -> str:
    """根据消息文本的关键词计数推断"天气"(无 LLM,纯规则)。"""
    if not messages:
        return random.choice(["晴", "多云", "阴", "多云转晴"])

    content = " ".join(_msg_text(m) for m in messages)
    happy_kw = ["哈哈", "笑", "开心", "高兴", "棒", "好", "赞", "爱", "喜欢"]
    sad_kw = ["难过", "伤心", "哭", "痛苦", "失望"]
    angry_kw = ["无语", "醉了", "服了", "烦", "气", "怒"]
    calm_kw = ["平静", "安静", "淡定", "还好", "一般"]
    happy = sum(1 for w in happy_kw if w in content)
    sad = sum(1 for w in sad_kw if w in content)
    angry = sum(1 for w in angry_kw if w in content)
    calm = sum(1 for w in calm_kw if w in content)

    if happy >= 2:
        return "晴"
    if happy >= 1:
        return "多云转晴"
    if sad >= 1:
        return "雨"
    if angry >= 1:
        return "阴"
    if calm >= 1:
        return "多云"
    return "多云"
