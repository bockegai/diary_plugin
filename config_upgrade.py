"""配置升级模块 — 与 MaiBot 本体同款方案。

每次插件加载时检查 config.toml 是否存在注释,
若 SDK 自动合并后文件缺少注释(如字段说明),则用 tomlkit 全量重建,
注释来自 ``Field(description=...)``。
"""

from pathlib import Path

from maibot_sdk import PluginConfigBase
import tomlkit

from .config import CONFIG_VERSION, PLUGIN_VERSION, DiaryPluginConfig


def needs_rewrite(config_path: Path) -> bool:
    """检查 config.toml 是否需要重建(缺少注释)。"""
    if not config_path.exists():
        return False
    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception:
        return False
    return not any(line.lstrip().startswith("#") for line in text.splitlines())


def upgrade_and_write(config_path: Path) -> bool:
    """全量重建 config.toml 并写入磁盘。返回 True 表示写入了新文件。"""
    # 1. 读取现有配置(保留用户设置值)
    if config_path.exists():
        existing_doc = tomlkit.loads(config_path.read_text(encoding="utf-8"))
        existing_data = existing_doc.unwrap()
    else:
        existing_data = {}

    # 2. 旧字段兼容 — napcat_* → http_*
    qz = existing_data.get("qzone_publishing")
    if isinstance(qz, dict):
        for old_key, new_key in [("napcat_host", "http_host"), ("napcat_port", "http_port"), ("napcat_token", "http_token")]:
            if old_key in qz and new_key not in qz:
                qz[new_key] = qz[old_key]

    # 3. 反序列化 → Pydantic(现存值保留,缺失字段用 default)
    config_instance = DiaryPluginConfig.model_validate(existing_data)
    config_dict = config_instance.model_dump()

    # 强制更新版本号到当前值(validate 保留文件旧值,不替换为 default)
    config_dict["plugin"]["version"] = PLUGIN_VERSION
    config_dict["plugin"]["config_version"] = CONFIG_VERSION

    # 3. 按 __ui_order__ 排序 sections
    sorted_sections = sorted(
        DiaryPluginConfig.model_fields.items(),
        key=lambda item: getattr(item[1].annotation, "__ui_order__", 99)
        if isinstance(item[1].annotation, type) and issubclass(item[1].annotation, PluginConfigBase)
        else 99,
    )

    # 4. 用 tomlkit 构建带注释的文档
    doc = tomlkit.document()
    for section_name, section_field in sorted_sections:
        section_type = section_field.annotation
        if not isinstance(section_type, type) or not issubclass(section_type, PluginConfigBase):
            continue

        section_defaults = config_dict.get(section_name)
        if not isinstance(section_defaults, dict):
            section_defaults = {}

        if len(doc) > 0:
            doc.add(tomlkit.nl())

        table = tomlkit.table()
        for field_name, field_info in section_type.model_fields.items():
            description = field_info.description or field_name
            value = section_defaults.get(field_name)
            table.add(tomlkit.comment(description))
            table.add(field_name, value)
        doc.add(section_name, table)

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True
