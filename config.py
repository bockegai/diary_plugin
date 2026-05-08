"""diary_plugin 配置模型。

按 PluginConfigBase 拆分为 6 个 section。注意 ``[custom_model]`` section 名
故意不叫 ``model_config`` —— Pydantic v2 把这个名字保留给 BaseModel 的元数据
属性,不能用作字段名。
"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础信息"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    name: str = Field(default="diary_plugin", description="插件名称")
    version: str = Field(default="3.0.0", description="插件版本")
    config_version: str = Field(default="3.0.0", description="配置版本(Runner 用于兼容性校验)")
    enabled: bool = Field(default=True, description="是否启用插件")
    admin_qqs: list[int] = Field(
        default_factory=list,
        description="管理员 QQ 号列表,用于使用 /diary generate / list / debug 等管理命令",
    )
    enable_tool: bool = Field(
        default=False,
        description="是否注册 emotion_analysis Tool 给 LLM 调用",
    )
    enable_command: bool = Field(
        default=True,
        description="是否注册 /diary 命令",
    )


class DiaryGenerationSection(PluginConfigBase):
    """日记生成相关配置"""

    __ui_label__ = "日记生成"
    __ui_icon__ = "book"
    __ui_order__ = 1

    min_message_count: int = Field(default=3, description="生成日记所需的最少消息总数")
    min_messages_per_chat: int = Field(default=3, description="单个聊天少于此条数则该聊天消息不参与日记生成")
    style: Literal["diary", "qqzone", "custom"] = Field(
        default="diary",
        description="生成样式: diary(日记) | qqzone(QQ 空间说说) | custom(自定义模板)",
    )
    custom_prompt: str = Field(
        default="",
        description=(
            "当 style=custom 时使用的模板。可用占位符: {date},{timeline},"
            "{date_with_weather},{target_length},{personality_desc},{style},{name}"
        ),
    )
    enable_style_send: bool = Field(
        default=False,
        description=(
            "是否开启风格化回复改写。本期重写后该能力暂未接入新 SDK,"
            "开关保留但 fallback 为普通文本输出。"
        ),
    )


class QzonePublishingSection(PluginConfigBase):
    """QQ 空间发布配置"""

    __ui_label__ = "QQ 空间发布"
    __ui_icon__ = "share-2"
    __ui_order__ = 2

    qzone_min_word_count: int = Field(default=150, ge=20, le=8000, description="最小字数,范围 20-8000")
    qzone_max_word_count: int = Field(default=350, ge=20, le=8000, description="最大字数,范围 20-8000,必须 ≥ 最小值")
    napcat_host: str = Field(default="127.0.0.1", description="Napcat 服务地址,Docker 环境可使用 'napcat'")
    napcat_port: str = Field(default="9998", description="Napcat 服务端口")
    napcat_token: str = Field(default="", description="Napcat 认证 Token,在 Napcat WebUI 网络配置中设置;为空则不使用 token")


class CustomModelSection(PluginConfigBase):
    """自定义模型配置(直连第三方 OpenAI 兼容 API)。

    section 名称故意叫 ``custom_model`` 而非 ``model_config`` —— 后者被
    Pydantic v2 保留为 BaseModel 元数据属性。
    """

    __ui_label__ = "自定义模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 3

    use_custom_model: bool = Field(default=False, description="启用自定义模型(否则使用下方 default_model 走系统 task)")
    api_url: str = Field(
        default="http://rinkoai.com/v1",
        description=(
            "OpenAI 兼容格式的 API 地址。仅支持 OpenAI 协议,不支持 Gemini / Claude 原生格式。"
            "推荐站点: http://rinkoai.com/pricing"
        ),
    )
    api_key: str = Field(default="your-rinko-key-here", description="API 密钥")
    model_name: str = Field(default="Pro/deepseek-ai/DeepSeek-V3", description="模型名称")
    temperature: float = Field(default=0.7, description="生成温度")
    api_timeout: int = Field(default=300, ge=1, le=6000, description="API 调用超时(秒),大量聊天记录建议设置更长")


class DefaultModelSection(PluginConfigBase):
    """系统默认模型配置(走 ctx.llm.generate 时使用)。

    显式声明 model 参数,避免 host ``resolve_task_name("")`` 字母序回退到
    ``embedding`` task。
    """

    __ui_label__ = "默认模型"
    __ui_icon__ = "brain"
    __ui_order__ = 4

    model_name: Literal["replyer", "utils", "planner", "vlm"] = Field(
        default="replyer",
        description="系统模型 task,与 host model_configs.py 的 chat 类 task 对齐",
    )
    temperature: float = Field(default=0.7, description="生成温度")
    llm_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description=(
            "单次 LLM 调用外层超时(秒)。"
            "⚠️ host RPC 桥接层硬上限 30s,本字段 > 30 时仍会被 RPC 30s 先触发 E_TIMEOUT。"
            "日记 prompt 较长经常超过 30s,建议改走 [custom_model].use_custom_model=true 直连。"
        ),
    )


class ScheduleSection(PluginConfigBase):
    """定时任务配置"""

    __ui_label__ = "定时任务"
    __ui_icon__ = "clock"
    __ui_order__ = 5

    schedule_time: str = Field(default="23:30", description="每日生成日记的时间 (HH:MM 格式)")
    timezone: str = Field(default="Asia/Shanghai", description="时区设置")
    filter_mode: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="过滤模式: whitelist(白名单) | blacklist(黑名单)",
    )
    target_chats: list[str] = Field(
        default_factory=list,
        description=(
            '目标列表,格式: ["group:群号", "private:用户qq号"]。'
            "白名单模式空列表=禁用定时任务;黑名单模式空列表=处理全部聊天。"
        ),
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class DiaryPluginConfig(PluginConfigBase):
    """diary_plugin 顶层配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    diary_generation: DiaryGenerationSection = Field(default_factory=DiaryGenerationSection)
    qzone_publishing: QzonePublishingSection = Field(default_factory=QzonePublishingSection)
    custom_model: CustomModelSection = Field(default_factory=CustomModelSection)
    default_model: DefaultModelSection = Field(default_factory=DefaultModelSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
