"""pytest 配置。把插件根目录的父目录加到 sys.path,使 ``import diary_plugin.*`` 工作。"""

import os
import sys

# diary_plugin/ 目录的父目录(即 plugins/)需要在 sys.path 上,
# 这样 `import diary_plugin.config` 才能解析。
_PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PLUGINS_DIR = os.path.dirname(_PLUGIN_DIR)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
