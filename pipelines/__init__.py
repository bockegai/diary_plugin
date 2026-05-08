"""diary_plugin 业务逻辑 pipelines。

公共导出:
- DiaryStorage:          JSON 文件落库
- QzonePublisher:        QQ 空间发布(Napcat 协议)
- LLMRunner / LLMCallError: ctx.llm.generate 包装
- ChatResolver:          group/private → session_id
- MessageFetcher:        ctx.message 抓取 + 黑/白名单 + 单聊条数过滤
- TimelineBuilder:       聊天时间线构建 + 图片识别
- weather_by_emotion:    根据消息关键词推断"天气"
- build_diary_prompt / build_qqzone_prompt / build_custom_prompt
- DiaryPipeline:         主流程 orchestrator
"""

from .chat_resolver import ChatResolver, parse_target_config, resolve_filter_strategy
from .diary_pipeline import DiaryPipeline
from .llm_runner import LLMCallError, LLMRunner
from .message_fetcher import MessageFetcher
from .prompts import build_custom_prompt, build_diary_prompt, build_qqzone_prompt
from .qzone_publisher import QzonePublisher
from .storage import DiaryStorage
from .timeline_builder import TimelineBuilder, weather_by_emotion

__all__ = [
    "ChatResolver",
    "DiaryPipeline",
    "DiaryStorage",
    "LLMCallError",
    "LLMRunner",
    "MessageFetcher",
    "QzonePublisher",
    "TimelineBuilder",
    "build_custom_prompt",
    "build_diary_prompt",
    "build_qqzone_prompt",
    "parse_target_config",
    "resolve_filter_strategy",
    "weather_by_emotion",
]
