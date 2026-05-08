"""RPC envelope 剥皮工具。

新版 SDK / Runner 在某些 capability 调用上返回双层 envelope:
    {"success": True, "result": {"success": True, "messages": [...]}}
本模块递归把 ``{success, result}`` 信封脱掉。
"""

from typing import Any


def peel_envelope(result: Any, *, max_depth: int = 4) -> Any:
    """递归脱掉 ``{"success": ..., "result": <inner>}`` 信封。"""
    for _ in range(max_depth):
        if not isinstance(result, dict):
            return result
        if "result" not in result or "success" not in result:
            return result
        inner = result["result"]
        if inner is None:
            return result
        result = inner
    return result
