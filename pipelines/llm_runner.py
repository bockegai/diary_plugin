"""LLM 调用包装。

设计要点:
- 必须显式传 ``model=`` 参数,否则 host ``resolve_task_name("")`` 字母序回退到
  ``embedding`` task,导致 chat completion 失败。
- 区分"调用失败"(异常 / success=False / 超时) vs "模型返空响应"。

⚠️ 已知限制:host 侧 RPC 桥接层硬编码 30s 超时
   (src/plugin_runtime/runner/rpc_client.py:171 timeout_ms=30000)
   且 SDK ctx.llm.generate 当前没有暴露 timeout_ms 参数,
   ``llm_timeout_seconds`` > 30 时仍会被 RPC 层 30s 先触发 E_TIMEOUT。
   日记任务因 prompt 长(含大量聊天记录)经常 > 30s,建议改走
   custom_model 路径(httpx 直连 OpenAI 兼容 API,不受 RPC 约束)。
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from ._envelope import peel_envelope

if TYPE_CHECKING:
    from maibot_sdk import PluginContext

    from ..config import DefaultModelSection

logger = logging.getLogger(__name__)


class LLMCallError(RuntimeError):
    """LLM 调用层失败(异常 / success=False / 超时)。"""


def _is_rpc_timeout(exc: BaseException) -> bool:
    """识别 host 侧 RPC E_TIMEOUT(无法 import RPCError,只能字符串匹配)。"""
    msg = str(exc)
    return "E_TIMEOUT" in msg or "cap.call 超时" in msg


class LLMRunner:
    """``ctx.llm.generate`` 的薄包装。"""

    def __init__(self, ctx: "PluginContext", model_config: "DefaultModelSection") -> None:
        self._ctx = ctx
        self._config = model_config

    async def generate(self, prompt: str) -> str:
        """生成文本。返回空串表示模型返空,失败抛 LLMCallError。"""
        if not prompt or not prompt.strip():
            logger.warning("prompt 为空,跳过 LLM 调用")
            return ""

        target_model = str(self._config.model_name or "replyer")
        temperature = self._config.temperature
        timeout = max(int(self._config.llm_timeout_seconds or 60), 1)
        logger.info(
            "调用 ctx.llm.generate model=%s temperature=%s prompt_len=%d timeout=%ds "
            "(注意: host RPC 层硬上限 30s,超过则切 custom_model 直连)",
            target_model,
            temperature,
            len(prompt),
            timeout,
        )

        try:
            result = await asyncio.wait_for(
                self._ctx.llm.generate(
                    prompt=prompt,
                    model=target_model,
                    temperature=temperature,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.error("ctx.llm.generate 外层超时(%ds)", timeout)
            raise LLMCallError(f"LLM 调用超时 ({timeout}s)") from exc
        except Exception as exc:
            if _is_rpc_timeout(exc):
                logger.error(
                    "RPC 桥接层 30s 硬超时(host 侧限制): %s。"
                    "建议在 [custom_model] 启用 use_custom_model=true 走 httpx 直连",
                    exc,
                )
                raise LLMCallError(
                    "RPC 30s 硬超时(host 侧限制,llm_timeout_seconds 配置无法突破)。"
                    "请在 config.toml 启用 [custom_model].use_custom_model=true 走直连模式。"
                ) from exc
            logger.error("ctx.llm.generate 抛异常: %s", exc, exc_info=True)
            raise LLMCallError(f"LLM 调用异常: {exc}") from exc

        result = peel_envelope(result)
        if not isinstance(result, dict):
            raise LLMCallError(f"LLM 返回非 dict: {type(result).__name__}")

        success = bool(result.get("success", False))
        response_text = str(result.get("response") or "")
        if not success:
            err = result.get("error") or "<no error key>"
            logger.error("LLM 调用失败 model=%s error=%s", target_model, err)
            raise LLMCallError(f"LLM 调用失败: {err}")
        return response_text.strip()
