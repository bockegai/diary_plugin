"""diary_plugin 主入口

业务逻辑全部抽到 ``pipelines/`` 子模块,本文件只负责装配 + 派发 + 调度器。
"""

import asyncio
import contextlib
import datetime
import logging
import re
from typing import Any, Optional

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import DiaryPluginConfig
from .pipelines import (
    ChatResolver,
    DiaryPipeline,
    DiaryStorage,
    LLMRunner,
    MessageFetcher,
    QzonePublisher,
)
from .pipelines._envelope import peel_envelope
from .utils.date import format_date_str

logger = logging.getLogger(__name__)


class DiaryPlugin(MaiBotPlugin):
    """日记插件主类。"""

    config_model = DiaryPluginConfig

    _scheduler_task: Optional[asyncio.Task]
    _pipeline: Optional[DiaryPipeline]
    _storage: Optional[DiaryStorage]
    _chat_resolver: Optional[ChatResolver]
    _message_fetcher: Optional[MessageFetcher]
    _llm_runner: Optional[LLMRunner]
    _qzone_publisher: Optional[QzonePublisher]

    def __init__(self) -> None:
        super().__init__()
        self._scheduler_task = None
        self._pipeline = None
        self._storage = None
        self._chat_resolver = None
        self._message_fetcher = None
        self._llm_runner = None
        self._qzone_publisher = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        cfg = self.config
        self.ctx.logger.info(
            "diary_plugin v%s 已加载 (style=%s, schedule_time=%s, filter_mode=%s, "
            "use_custom_model=%s, default_model=%s)",
            cfg.plugin.version,
            cfg.diary_generation.style,
            cfg.schedule.schedule_time,
            cfg.schedule.filter_mode,
            cfg.custom_model.use_custom_model,
            cfg.default_model.model_name,
        )
        await self._build_pipeline()
        if self._should_run_scheduler():
            self._scheduler_task = asyncio.create_task(self._schedule_loop())
            self.ctx.logger.info(
                "定时任务已启动:每日 %s (%s)",
                cfg.schedule.schedule_time,
                cfg.schedule.timezone,
            )
        else:
            self.ctx.logger.info("定时任务未启动(filter_mode=%s,target_chats 为空)", cfg.schedule.filter_mode)

    async def on_unload(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
        self.ctx.logger.info("diary_plugin 已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict,
        version: str,
    ) -> None:
        del config_data
        self.ctx.logger.info("配置更新: scope=%s version=%s,重建 pipeline", scope, version)
        await self._build_pipeline()

    async def _build_pipeline(self) -> None:
        """装配 pipelines。"""
        try:
            uin = await self._resolve_bot_qq_int()
            self._storage = DiaryStorage()
            self._chat_resolver = ChatResolver(self.ctx)
            self._message_fetcher = MessageFetcher(self.ctx, self._chat_resolver)
            self._llm_runner = LLMRunner(self.ctx, self.config.default_model)
            self._qzone_publisher = QzonePublisher(uin=uin) if uin > 0 else None
            self._pipeline = DiaryPipeline(
                ctx=self.ctx,
                config=self.config,
                storage=self._storage,
                message_fetcher=self._message_fetcher,
                llm_runner=self._llm_runner,
                qzone_publisher=self._qzone_publisher,
            )
            self.ctx.logger.info(
                "pipeline 装配完成 (uin=%s, qzone=%s)",
                uin if uin else "未配置",
                "可用" if self._qzone_publisher else "禁用",
            )
        except Exception as exc:
            self.ctx.logger.error("装配 pipeline 失败: %s", exc, exc_info=True)
            self._pipeline = None

    async def _resolve_bot_qq_int(self) -> int:
        try:
            value = await self.ctx.config.get("bot.qq_account", 0)
        except Exception as exc:
            logger.warning("ctx.config.get(bot.qq_account) 失败: %s", exc)
            return 0
        value = peel_envelope(value)
        if isinstance(value, dict):
            value = value.get("value", 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _ensure_pipeline_ready(self) -> bool:
        return self._pipeline is not None

    # ===== 调度器 =====

    def _should_run_scheduler(self) -> bool:
        cfg = self.config.schedule
        if cfg.filter_mode == "whitelist" and not cfg.target_chats:
            return False
        return True

    def _get_now(self) -> datetime.datetime:
        try:
            import pytz
            tz = pytz.timezone(self.config.schedule.timezone)
            return datetime.datetime.now(tz)
        except Exception:
            return datetime.datetime.now()

    def _next_run_seconds(self) -> float:
        now = self._get_now()
        try:
            hour, minute = map(int, self.config.schedule.schedule_time.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 23, 30
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + datetime.timedelta(days=1)
        return (next_run - now).total_seconds()

    async def _schedule_loop(self) -> None:
        while True:
            try:
                wait = self._next_run_seconds()
                self.ctx.logger.info("下次定时日记: +%ds", int(wait))
                await asyncio.sleep(wait)
                if not self._ensure_pipeline_ready():
                    self.ctx.logger.warning("pipeline 未就绪,跳过此次执行")
                    await asyncio.sleep(60)
                    continue
                await self._pipeline.generate_and_publish_for_today()
            except asyncio.CancelledError:
                self.ctx.logger.info("调度器收到取消信号")
                break
            except Exception as exc:
                self.ctx.logger.error("调度器异常: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    # ===== @Tool emotion_analysis =====

    @Tool(
        "emotion_analysis",
        description="分析聊天记录的情感色彩,识别开心、无语、吐槽、感动等情绪",
        parameters=[
            ToolParameterInfo(
                name="messages",
                param_type=ToolParamType.STRING,
                description="聊天记录文本",
                required=True,
            ),
            ToolParameterInfo(
                name="analysis_type",
                param_type=ToolParamType.STRING,
                description="分析类型: emotion(情感) 或 topic(主题)",
                required=False,
            ),
        ],
    )
    async def handle_emotion_analysis(
        self,
        messages: str = "",
        analysis_type: str = "emotion",
        **kwargs: Any,
    ) -> dict:
        del kwargs
        if not self.config.plugin.enable_tool:
            return {"name": "emotion_analysis", "content": "情感分析 Tool 已禁用"}
        text = (messages or "").strip()
        if not text:
            return {"name": "emotion_analysis", "content": "没有消息内容可分析"}

        if analysis_type == "emotion":
            emotions = []
            if any(w in text for w in ["哈哈", "笑", "开心", "高兴"]):
                emotions.append("开心")
            if any(w in text for w in ["无语", "醉了", "服了"]):
                emotions.append("无语")
            if any(w in text for w in ["吐槽", "抱怨", "烦"]):
                emotions.append("吐槽")
            if any(w in text for w in ["感动", "温暖", "暖心"]):
                emotions.append("感动")
            content = f"检测到的情感: {'、'.join(emotions) if emotions else '平静'}"
        else:
            content = "聊天主题: 日常对话"
        return {"name": "emotion_analysis", "content": content}

    # ===== @Command /diary =====

    @Command(
        "diary",
        description="日记管理命令(/diary generate|list|view|debug|help)",
        pattern=r"^\s*/\s*diary\s+(?P<action>list|generate|help|debug|view)(?:\s+(?P<param>.+))?\s*$",
    )
    async def handle_diary(
        self,
        **kwargs: Any,
    ) -> tuple:
        if not self.config.plugin.enable_command:
            return False, "diary 命令已禁用", True

        matched = kwargs.get("matched_groups") or {}
        action = (matched.get("action") or "").strip()
        param_raw = matched.get("param")
        param = re.sub(r"\s+", " ", param_raw.strip()) if isinstance(param_raw, str) else ""

        stream_id = str(kwargs.get("stream_id", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")

        # 权限检查
        if action not in ("view", "help"):
            admins = {str(x) for x in self.config.plugin.admin_qqs}
            if user_id not in admins:
                if group_id:
                    return False, "无权限(群聊静默)", True
                if stream_id:
                    await self.ctx.send.text("❌ 您没有权限使用此命令。", stream_id)
                return False, "无权限", True

        if not self._ensure_pipeline_ready():
            if stream_id:
                await self.ctx.send.text("⚠️ 日记插件未就绪", stream_id)
            return False, "pipeline 未就绪", True

        try:
            if action == "generate":
                return await self._cmd_generate(param, stream_id, group_id)
            if action == "view":
                return await self._cmd_view(param, stream_id)
            if action == "list":
                return await self._cmd_list(param, stream_id)
            if action == "debug":
                return await self._cmd_debug(param, stream_id, group_id)
            if action == "help":
                return await self._cmd_help(stream_id)
            return False, f"未知子命令: {action}", True
        except Exception as exc:
            self.ctx.logger.error("/diary %s 执行失败: %s", action, exc, exc_info=True)
            if stream_id:
                await self.ctx.send.text(f"❌ 命令执行出错: {exc}", stream_id)
            return False, str(exc), True

    # ===== 子命令实现 =====

    async def _cmd_generate(self, param: str, stream_id: str, group_id: str) -> tuple:
        try:
            date = format_date_str(param if param else datetime.datetime.now())
        except ValueError as exc:
            if stream_id:
                await self.ctx.send.text(
                    f"❌ 日期格式错误: {exc}\n\n💡 支持: 2025-08-24 / 2025/08/24 / 2025.08.24",
                    stream_id,
                )
            return False, "日期格式错误", True

        if stream_id:
            await self.ctx.send.text(f"我正在写 {date} 的日记...", stream_id)

        # generate 命令忽略黑白名单。如果在群聊中执行,只取该群的消息;否则全局
        if group_id:
            try:
                stream = await self.ctx.chat.get_stream_by_group_id(group_id)
                stream = peel_envelope(stream)
                if isinstance(stream, dict):
                    target_stream = stream.get("stream", stream)
                    if isinstance(target_stream, dict):
                        sid = target_stream.get("session_id") or target_stream.get("stream_id")
                        if sid:
                            messages = await self._pipeline.fetch_messages_for_date(
                                date, target_chats=[sid]
                            )
                        else:
                            messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True)
                    else:
                        messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True)
                else:
                    messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True)
            except Exception as exc:
                self.ctx.logger.warning("群聊 chat 查询失败,降级全局: %s", exc)
                messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True)
        else:
            messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True)

        success, result = await self._pipeline.generate_from_messages(date, messages, force_50k=True)
        if not success:
            if stream_id:
                await self.ctx.send.text(f"❌ 生成失败: {result}", stream_id)
            return False, result, True

        if stream_id:
            await self.ctx.send.text(f"日记生成成功！正在发布到 QQ 空间\n{date}:\n{result}", stream_id)
        publish_ok = await self._pipeline.publish_to_qzone(date, result)
        if stream_id:
            if publish_ok:
                await self.ctx.send.text("已成功发布到 QQ 空间！", stream_id)
            else:
                await self.ctx.send.text(
                    "⚠️ QQ 空间发布失败,可能原因:\n1. Napcat 服务未启动\n2. 端口配置错误\n3. QQ 空间权限问题",
                    stream_id,
                )
        return True, result, True

    async def _cmd_view(self, param: str, stream_id: str) -> tuple:
        args = param.split() if param else []
        try:
            date = format_date_str(args[0] if args else datetime.datetime.now())
        except ValueError as exc:
            if stream_id:
                await self.ctx.send.text(f"❌ 日期格式错误: {exc}", stream_id)
            return False, "日期格式错误", True

        diaries = await self._storage.get_diaries_by_date(date)
        if not diaries:
            if stream_id:
                await self.ctx.send.text(f"📭 没有找到 {date} 的日记", stream_id)
            return True, "无日记", True

        diaries.sort(key=lambda d: d.get("generation_time", 0))
        if len(args) > 1 and args[1].isdigit():
            idx = int(args[1]) - 1
            if 0 <= idx < len(diaries):
                d = diaries[idx]
                gt = datetime.datetime.fromtimestamp(d.get("generation_time", 0))
                status = "✅已发布" if d.get("is_published_qzone") else "❌未发布"
                if stream_id:
                    await self.ctx.send.text(
                        f"📖 {date} 日记 {idx+1} ({gt.strftime('%H:%M')}) | "
                        f"{d.get('word_count', 0)}字 | {status}:\n\n{d.get('diary_content', '')}",
                        stream_id,
                    )
            else:
                if stream_id:
                    await self.ctx.send.text("❌ 编号无效", stream_id)
            return True, "查看完成", True

        lines = []
        for i, d in enumerate(diaries, 1):
            gt = datetime.datetime.fromtimestamp(d.get("generation_time", 0))
            status = "✅已发布" if d.get("is_published_qzone") else "❌未发布"
            lines.append(f"{i}. {gt.strftime('%H:%M')} | {d.get('word_count', 0)}字 | {status}")
        if stream_id:
            await self.ctx.send.text(
                f"📅 {date} 的日记列表:\n" + "\n".join(lines)
                + "\n\n输入 /diary view {日期} {编号} 查看具体内容",
                stream_id,
            )
        return True, "列表完成", True

    async def _cmd_list(self, param: str, stream_id: str) -> tuple:
        if param == "all":
            stats = await self._storage.get_stats()
            diaries = await self._storage.list_diaries(limit=0)
            if not diaries:
                if stream_id:
                    await self.ctx.send.text("📭 还没有任何日记记录", stream_id)
                return True, "空", True
            success_count = sum(1 for d in diaries if d.get("is_published_qzone"))
            failed_count = len(diaries) - success_count
            success_rate = success_count / len(diaries) * 100 if diaries else 0
            dates = sorted({d.get("date", "") for d in diaries if d.get("date")})
            date_range = f"{dates[0]} ~ {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else "无")
            if stream_id:
                await self.ctx.send.text(
                    f"📚 日记详细统计:\n📖 总日记数: {stats['total_count']}篇\n"
                    f"📝 总字数: {stats['total_words']}字 (平均 {stats['avg_words']}字/篇)\n"
                    f"📅 日期范围: {date_range}\n"
                    f"📱 发布: {success_count} 成功 / {failed_count} 失败 ({success_rate:.1f}%)",
                    stream_id,
                )
            return True, "统计完成", True

        if param and re.match(r"\d{4}-\d{1,2}-\d{1,2}", param):
            try:
                date = format_date_str(param)
            except ValueError as exc:
                if stream_id:
                    await self.ctx.send.text(f"❌ 日期格式错误: {exc}", stream_id)
                return False, "日期格式错误", True
            diaries = await self._storage.get_diaries_by_date(date)
            if not diaries:
                if stream_id:
                    await self.ctx.send.text(f"📭 没有找到 {date} 的日记", stream_id)
                return True, "空", True
            total_words = sum(d.get("word_count", 0) for d in diaries)
            success_count = sum(1 for d in diaries if d.get("is_published_qzone"))
            lines = []
            for i, d in enumerate(diaries, 1):
                gt = datetime.datetime.fromtimestamp(d.get("generation_time", 0))
                status = "✅已发布" if d.get("is_published_qzone") else "❌未发布"
                lines.append(f"{i}. {gt.strftime('%H:%M')} ({d.get('word_count', 0)}字) {status}")
            if stream_id:
                await self.ctx.send.text(
                    f"📅 {date} 日记概况:\n📝 共{len(diaries)}篇,总字数 {total_words}\n"
                    f"📱 已发布 {success_count}/{len(diaries)}\n\n"
                    + "\n".join(lines),
                    stream_id,
                )
            return True, "完成", True

        # 默认: 概览 + 最近10篇
        stats = await self._storage.get_stats()
        recent = await self._storage.list_diaries(limit=10)
        if not recent:
            if stream_id:
                await self.ctx.send.text("📭 还没有任何日记记录", stream_id)
            return True, "空", True
        lines = []
        for d in recent:
            status = "✅已发布" if d.get("is_published_qzone") else "❌未发布"
            lines.append(f"📅 {d.get('date', '')} ({d.get('word_count', 0)}字) {status}")
        if stream_id:
            await self.ctx.send.text(
                f"📚 日记概览:\n📖 总日记数: {stats['total_count']}篇\n"
                f"📝 总字数: {stats['total_words']}字 (平均 {stats['avg_words']}字/篇)\n"
                f"📅 最新日记: {stats['latest_date']}\n\n📋 最近 10 篇:\n"
                + "\n".join(lines)
                + "\n\n💡 /diary list [日期] 查看指定日期 / /diary list all 查看详细",
                stream_id,
            )
        return True, "概览完成", True

    async def _cmd_debug(self, param: str, stream_id: str, group_id: str) -> tuple:
        try:
            date = format_date_str(param if param else datetime.datetime.now())
        except ValueError as exc:
            if stream_id:
                await self.ctx.send.text(f"❌ 日期格式错误: {exc}", stream_id)
            return False, "日期格式错误", True

        bot_qq = await self._resolve_bot_qq_int()
        bot_qq_str = str(bot_qq) if bot_qq else "未配置"
        try:
            nickname_raw = await self.ctx.config.get("bot.nickname", "麦麦")
            nickname_raw = peel_envelope(nickname_raw)
            if isinstance(nickname_raw, dict):
                nickname_raw = nickname_raw.get("value", "麦麦")
            bot_nickname = str(nickname_raw or "麦麦")
        except Exception:
            bot_nickname = "麦麦"

        # 取当日消息(忽略黑白名单,如果在群聊则限定该群)
        try:
            if group_id:
                stream = await self.ctx.chat.get_stream_by_group_id(group_id)
                stream = peel_envelope(stream)
                target_stream = stream.get("stream", stream) if isinstance(stream, dict) else None
                if isinstance(target_stream, dict):
                    sid = target_stream.get("session_id") or target_stream.get("stream_id")
                else:
                    sid = None
                if sid:
                    messages = await self._pipeline.fetch_messages_for_date(date, target_chats=[sid], ignore_min_messages_per_chat=True)
                    context_desc = f"【本群】({group_id} → {sid})"
                else:
                    messages = []
                    context_desc = f"【本群】({group_id} → 未找到)"
            else:
                messages = await self._pipeline.fetch_messages_for_date(date, ignore_filter=True, ignore_min_messages_per_chat=True)
                context_desc = "【全局日记】"
        except Exception as exc:
            self.ctx.logger.error("debug 取消息失败: %s", exc, exc_info=True)
            messages = []
            context_desc = "【取消息失败】"

        bot_msgs = 0
        user_msgs = 0
        chat_ids = set()
        for m in messages:
            info = (m.get("message_info") or {}).get("user_info") or {}
            uid = str(info.get("user_id", "") or "")
            if uid == bot_qq_str:
                bot_msgs += 1
            else:
                user_msgs += 1
            sid = m.get("session_id")
            if sid:
                chat_ids.add(sid)

        debug_text = (
            f"🔍 Bot 消息读取调试 ({date}):\n\n"
            f"🤖 Bot 信息:\n- QQ 号: {bot_qq_str}\n- 昵称: {bot_nickname}\n\n"
            f"📅 {date} 消息统计 {context_desc}:\n"
            f"- 活跃聊天: {len(chat_ids)} 个\n"
            f"- 用户消息: {user_msgs} 条\n"
            f"- Bot 消息: {bot_msgs} 条\n"
            f"- 总计: {len(messages)} 条"
        )
        if stream_id:
            await self.ctx.send.text(debug_text, stream_id)
        return True, "调试完成", True

    async def _cmd_help(self, stream_id: str) -> tuple:
        text = (
            "📖 日记插件帮助\n\n"
            "👥 所有用户可用:\n"
            "/diary help - 显示帮助\n"
            "/diary view [日期] [编号] - 查看日记\n\n"
            "🔒 管理员专用:\n"
            "/diary generate [日期] - 生成日记\n"
            "/diary list [日期|all] - 日记列表/统计\n"
            "/diary debug [日期] - 调试信息\n\n"
            "📅 日期格式: YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD"
        )
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, "帮助完成", True


def create_plugin() -> DiaryPlugin:
    """Runner 通过此工厂函数实例化插件。"""
    return DiaryPlugin()
