"""日记生成主流程 orchestrator。

把消息抓取 / 时间线构建 / prompt 生成 / 模型调用 / 截断 / 落库 / 发布
串成一个完整流程,被 Command 和 Scheduler 共用。
"""

import asyncio
import datetime
import logging
import random
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from ..utils.tokens import (
    MAX_DIARY_LENGTH,
    TOKEN_LIMIT_50K,
    estimate_tokens,
    smart_truncate,
    truncate_by_tokens,
)
from ..utils.date import date_with_weather as fmt_date_weather
from ._envelope import peel_envelope
from .llm_runner import LLMCallError, LLMRunner
from .message_fetcher import MessageFetcher
from .prompts import build_custom_prompt, build_diary_prompt, build_qqzone_prompt
from .qzone_publisher import QzonePublisher
from .storage import DiaryStorage
from .timeline_builder import TimelineBuilder, weather_by_emotion

if TYPE_CHECKING:
    from maibot_sdk import PluginContext
    from ..config import DiaryPluginConfig

logger = logging.getLogger(__name__)


async def _resolve_personality(ctx) -> Dict[str, str]:
    """从 ctx.config 取 bot 人设。"""
    async def _get(key: str, default: str) -> str:
        try:
            value = await ctx.config.get(key, default)
        except Exception as exc:
            logger.warning("ctx.config.get(%s) 失败: %s", key, exc)
            return default
        value = peel_envelope(value)
        if isinstance(value, dict):
            value = value.get("value", default)
        return str(value or default)

    return {
        "core": await _get("personality.personality", "是一个机器人助手"),
        "style": await _get("personality.reply_style", ""),
        "nickname": await _get("bot.nickname", ""),
    }


async def _resolve_bot_qq(ctx) -> str:
    """取 bot.qq_account 字符串。"""
    try:
        value = await ctx.config.get("bot.qq_account", 0)
    except Exception as exc:
        logger.warning("ctx.config.get(bot.qq_account) 失败: %s", exc)
        return ""
    value = peel_envelope(value)
    if isinstance(value, dict):
        value = value.get("value", 0)
    return str(value or "")


class DiaryPipeline:
    """日记生成主流程。"""

    def __init__(
        self,
        ctx,
        config,
        storage: DiaryStorage,
        message_fetcher: MessageFetcher,
        llm_runner: LLMRunner,
        qzone_publisher: Optional[QzonePublisher] = None,
    ) -> None:
        self._ctx = ctx
        self._config = config
        self._storage = storage
        self._fetcher = message_fetcher
        self._llm_runner = llm_runner
        self._qzone_publisher = qzone_publisher

    async def fetch_messages_for_date(
        self,
        date: str,
        end_hour: Optional[int] = None,
        end_minute: Optional[int] = None,
        target_chats: Optional[List[str]] = None,
        ignore_filter: bool = False,
        ignore_min_messages_per_chat: bool = False,
    ) -> List[Dict[str, Any]]:
        start_time, end_time = self._resolve_time_window(date, end_hour, end_minute)

        if target_chats:
            messages = await self._fetcher.fetch_for_chats(target_chats, start_time, end_time)
        elif ignore_filter:
            messages = await self._fetcher.fetch_all(start_time, end_time)
        else:
            messages = await self._fetcher.fetch_with_filter(
                self._config.schedule.filter_mode,
                self._config.schedule.target_chats,
                start_time,
                end_time,
            )

        if not ignore_min_messages_per_chat:
            min_per_chat = self._config.diary_generation.min_messages_per_chat
            if min_per_chat > 0:
                messages = MessageFetcher.filter_min_messages_per_chat(messages, min_per_chat)
        return messages

    def _resolve_time_window(
        self,
        date: str,
        end_hour: Optional[int] = None,
        end_minute: Optional[int] = None,
    ) -> Tuple[float, float]:
        """统一用 schedule.timezone 计算 [start_time, end_time] 时间戳。"""
        try:
            import pytz
            tz = pytz.timezone(self._config.schedule.timezone)
        except Exception:
            tz = None

        naive = datetime.datetime.strptime(date, "%Y-%m-%d")
        if tz is not None:
            date_obj = tz.localize(naive)
            now = datetime.datetime.now(tz)
        else:
            date_obj = naive
            now = datetime.datetime.now()

        start_time = date_obj.timestamp()
        if end_hour is not None and end_minute is not None:
            end_time = date_obj.replace(hour=end_hour, minute=end_minute, second=0).timestamp()
        elif now.strftime("%Y-%m-%d") == date:
            end_time = now.timestamp()
        else:
            end_time = (date_obj + datetime.timedelta(days=1)).timestamp()
        return start_time, end_time

    def _today_str(self) -> str:
        """schedule.timezone 下的 'YYYY-MM-DD'。"""
        try:
            import pytz
            tz = pytz.timezone(self._config.schedule.timezone)
            return datetime.datetime.now(tz).strftime("%Y-%m-%d")
        except Exception:
            return datetime.datetime.now().strftime("%Y-%m-%d")

    async def generate_from_messages(
        self,
        date: str,
        messages: List[Dict[str, Any]],
        force_50k: bool = True,
    ) -> Tuple[bool, str]:
        if len(messages) < self._config.diary_generation.min_message_count:
            return False, f"消息数量不足({len(messages)}条),无法生成日记"

        try:
            personality = await _resolve_personality(self._ctx)
            bot_qq = await _resolve_bot_qq(self._ctx)

            timeline_builder = TimelineBuilder(bot_qq_account=bot_qq)
            timeline = timeline_builder.build(messages)

            if force_50k and estimate_tokens(timeline) > TOKEN_LIMIT_50K:
                timeline = truncate_by_tokens(timeline, TOKEN_LIMIT_50K)

            weather = weather_by_emotion(messages)
            date_str = fmt_date_weather(date, weather)

            qzone_cfg = self._config.qzone_publishing
            min_wc = self._normalize_int(qzone_cfg.qzone_min_word_count, default=250, lo=20, hi=MAX_DIARY_LENGTH)
            max_wc = self._normalize_int(qzone_cfg.qzone_max_word_count, default=350, lo=20, hi=MAX_DIARY_LENGTH)
            if max_wc < min_wc:
                max_wc = min_wc
            target_length = random.randint(min_wc, max_wc)

            prompt = self._compose_prompt(
                date=date,
                timeline=timeline,
                date_str=date_str,
                target_length=target_length,
                personality=personality,
            )

            content = await self._call_model(prompt, timeline)
            if not content:
                await self._save_failed(date, weather, "模型返回空内容")
                return False, "模型生成日记失败(返空)"

            if len(content) > max_wc:
                content = smart_truncate(content, max_wc)

            await self._storage.save_diary({
                "date": date,
                "diary_content": content,
                "word_count": len(content),
                "generation_time": time.time(),
                "weather": weather,
                "bot_messages": timeline_builder.stats["bot_messages"],
                "user_messages": timeline_builder.stats["user_messages"],
                "is_published_qzone": False,
                "qzone_publish_time": None,
                "status": "生成成功",
                "error_message": "",
            })
            return True, content

        except LLMCallError as exc:
            await self._save_failed(date, "阴", f"LLM 调用失败: {exc}")
            return False, str(exc)
        except Exception as exc:
            logger.error("生成日记失败: %s", exc, exc_info=True)
            await self._save_failed(date, "阴", str(exc))
            return False, f"生成日记时出错: {exc}"

    async def publish_to_qzone(self, date: str, content: str) -> bool:
        if not self._qzone_publisher:
            logger.warning("qzone_publisher 未装配,跳过发布")
            return False

        cfg = self._config.qzone_publishing
        try:
            success = await self._qzone_publisher.publish(
                content,
                napcat_host=cfg.napcat_host,
                napcat_port=cfg.napcat_port,
                napcat_token=cfg.napcat_token,
            )
        except Exception as exc:
            logger.error("发布异常: %s", exc, exc_info=True)
            success = False

        diary_data = await self._storage.get_diary(date)
        if diary_data:
            if success:
                diary_data["is_published_qzone"] = True
                diary_data["qzone_publish_time"] = time.time()
                diary_data["status"] = "一切正常"
                diary_data["error_message"] = ""
            else:
                diary_data["is_published_qzone"] = False
                diary_data["status"] = "报错:发说说失败"
                diary_data["error_message"] = "原因:QQ 空间发布失败,可能是 cookie 过期或网络问题"
            await self._storage.save_diary(diary_data)
        return success

    async def generate_and_publish_for_today(self) -> Tuple[bool, str]:
        today = self._today_str()
        messages = await self.fetch_messages_for_date(today)
        success, result = await self.generate_from_messages(today, messages, force_50k=True)
        if not success:
            logger.error("定时日记生成失败 %s: %s", today, result)
            return success, result
        publish_ok = await self.publish_to_qzone(today, result)
        if publish_ok:
            logger.info("定时日记 %s 生成 + 发布成功 (%d 字)", today, len(result))
        else:
            logger.info("定时日记 %s 生成成功 (%d 字),QQ 空间发布失败", today, len(result))
        return success, result

    def _compose_prompt(
        self,
        *,
        date: str,
        timeline: str,
        date_str: str,
        target_length: int,
        personality: Dict[str, str],
    ) -> str:
        gen_cfg = self._config.diary_generation
        style = gen_cfg.style
        personality_desc = personality["core"]
        name = personality.get("nickname", "")

        if style == "custom":
            ctx = {
                "date": date,
                "timeline": timeline,
                "date_with_weather": date_str,
                "target_length": str(target_length),
                "personality_desc": personality_desc,
                "style": personality.get("style", ""),
                "name": name,
            }
            try:
                return build_custom_prompt(gen_cfg.custom_prompt, ctx)
            except ValueError as exc:
                logger.warning("custom_prompt 失败,降级 diary 模板: %s", exc)
                style = "diary"

        if style == "qqzone":
            return build_qqzone_prompt(
                date=date,
                timeline=timeline,
                date_with_weather=date_str,
                target_length=target_length,
                personality_desc=personality_desc,
                style_desc=personality.get("style", ""),
                name=name,
            )
        return build_diary_prompt(
            date=date,
            timeline=timeline,
            date_with_weather=date_str,
            target_length=target_length,
            personality_desc=personality_desc,
            style_desc=personality.get("style", ""),
            name=name,
        )

    async def _call_model(self, prompt: str, timeline: str) -> str:
        custom = self._config.custom_model
        if custom.use_custom_model:
            return await self._generate_with_custom_model(prompt)
        if estimate_tokens(timeline) > TOKEN_LIMIT_50K:
            truncated = truncate_by_tokens(timeline, TOKEN_LIMIT_50K)
            prompt = prompt.replace(timeline, truncated)
        return await self._llm_runner.generate(prompt)

    async def _generate_with_custom_model(self, prompt: str) -> str:
        cfg = self._config.custom_model
        if not cfg.api_key or cfg.api_key in ("your-rinko-key-here", "sk-your-siliconflow-key-here"):
            raise LLMCallError("自定义模型 API key 未配置")
        async with AsyncOpenAI(base_url=cfg.api_url, api_key=cfg.api_key) as client:
            try:
                completion = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=cfg.model_name,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=cfg.temperature,
                    ),
                    timeout=cfg.api_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise LLMCallError(f"自定义模型超时 ({cfg.api_timeout}s)") from exc
            except Exception as exc:
                raise LLMCallError(f"自定义模型调用异常: {exc}") from exc
            if not completion.choices:
                raise LLMCallError("自定义模型返回空 choices")
            content = completion.choices[0].message.content or ""
            return content.strip()

    async def _save_failed(self, date: str, weather: str, error_message: str) -> None:
        try:
            await self._storage.save_diary({
                "date": date,
                "diary_content": "",
                "word_count": 0,
                "generation_time": time.time(),
                "weather": weather,
                "bot_messages": 0,
                "user_messages": 0,
                "is_published_qzone": False,
                "qzone_publish_time": None,
                "status": "报错:生成失败",
                "error_message": f"原因:{error_message}",
            })
        except Exception as exc:
            logger.error("保存失败记录出错: %s", exc)

    @staticmethod
    def _normalize_int(value, *, default: int, lo: int, hi: int) -> int:
        if not isinstance(value, int):
            return default
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value
