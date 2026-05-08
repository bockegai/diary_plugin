"""diary_plugin 配置迁移脚本(v2.x → v3.0)。

主要变更:
- 删除 ``enable_action``(legacy Action 概念已移除)
- ``enable_syle_send`` → ``enable_style_send``(顺便修拼写)
- 新增 ``[default_model]`` section(替代硬编码 replyer)
- ``config_version`` → "3.0.0"

用法:
    python scripts/migrate_config.py [/path/to/config.toml]
不指定路径时迁移当前目录下的 ``config.toml``。原文件备份为 ``config.toml.bak``。
"""

import os
import re
import shutil
import sys


def migrate(path: str) -> bool:
    if not os.path.exists(path):
        print(f"[migrate] 找不到 {path},跳过")
        return False

    bak = path + ".bak"
    shutil.copyfile(path, bak)
    print(f"[migrate] 已备份 → {bak}")

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    changed = False

    # 1. enable_syle_send → enable_style_send
    new_text, n = re.subn(r"\benable_syle_send\b", "enable_style_send", text)
    if n:
        text = new_text
        changed = True
        print(f"[migrate] 重命名 enable_syle_send → enable_style_send ({n} 处)")

    # 2. 删除 enable_action 整行(包括注释行)
    lines = text.split("\n")
    out = []
    skip_next_blank = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^enable_action\s*=", stripped):
            print("[migrate] 删除 enable_action 配置行")
            changed = True
            # 同时删除紧邻的注释行(向上扫)
            while out and out[-1].strip().startswith("#") and "DiaryGeneratorAction" in out[-1]:
                out.pop()
            skip_next_blank = True
            continue
        if skip_next_blank and stripped == "":
            skip_next_blank = False
            continue
        skip_next_blank = False
        out.append(line)
    text = "\n".join(out)

    # 3. config_version → 3.0.0
    new_text, n = re.subn(
        r'^(\s*config_version\s*=\s*)"[^"]*"', r'\g<1>"3.0.0"', text, flags=re.MULTILINE
    )
    if n:
        text = new_text
        changed = True
        print(f"[migrate] config_version → 3.0.0")

    # 4. 添加 [default_model] section(若缺失)
    if "[default_model]" not in text:
        section = (
            "\n\n# 默认模型配置(走 ctx.llm.generate 时使用,显式 model 避开 host Bug C)\n"
            "[default_model]\n\n"
            "# 系统模型 task,与 host model_configs.py 的 chat 类 task 对齐\n"
            '# 可选: replyer | utils | planner | vlm\n'
            'model_name = "replyer"\n\n'
            "# 生成温度\n"
            "temperature = 0.7\n\n"
            "# 单次 LLM 调用超时(秒)\n"
            "llm_timeout_seconds = 120\n"
        )
        # 把它插到 [custom_model] section 之后(或文件末尾)
        marker = re.search(r"^\[custom_model\]", text, flags=re.MULTILINE)
        if marker:
            # 找到 [custom_model] 之后的下一个 section 起点
            after_idx = marker.end()
            next_section = re.search(r"^\[", text[after_idx:], flags=re.MULTILINE)
            if next_section:
                insert_at = after_idx + next_section.start()
                text = text[:insert_at] + section.lstrip("\n") + "\n\n" + text[insert_at:]
            else:
                text = text + section
        else:
            text = text + section
        changed = True
        print("[migrate] 已添加 [default_model] section")

    if not changed:
        print("[migrate] 无需变更")
        os.remove(bak)
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[migrate] 已写回 {path}")
    return True


def main() -> int:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
        path = os.path.normpath(path)
    print(f"[migrate] 目标: {path}")
    migrate(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
