"""token 估算与文本截断。"""

import re


# 默认日记长度上限(防止生成内容失控)
MAX_DIARY_LENGTH = 8000

# 50k token 限制(默认模型路径强制使用)
TOKEN_LIMIT_50K = 50000


def estimate_tokens(text: str) -> int:
    """中英混合 token 估算。中文 ≈ 1.5 字/token,英文 ≈ 4 字/token。"""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def truncate_by_tokens(text: str, max_tokens: int) -> str:
    """按 token 数截断,保持语句完整性,留 5% 余量。"""
    current = estimate_tokens(text)
    if current <= max_tokens:
        return text
    ratio = max_tokens / current
    target_length = int(len(text) * ratio * 0.95)
    truncated = text[:target_length]
    for i in range(len(truncated) - 1, len(truncated) // 2, -1):
        if truncated[i] in ("。", "！", "？", "\n"):
            truncated = truncated[: i + 1]
            break
    return truncated + "\n\n[聊天记录过长,已截断]"


def smart_truncate(text: str, max_length: int = MAX_DIARY_LENGTH) -> str:
    """按字符数智能截断,保持语句完整性。"""
    if len(text) <= max_length:
        return text
    for i in range(max_length - 3, max_length // 2, -1):
        if text[i] in ("。", "！", "？", "~"):
            return text[: i + 1]
    return text[: max_length - 3] + "..."
