"""日记 / QQ 空间 / 自定义 三套 prompt 模板。"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def build_diary_prompt(
    *,
    date: str,
    timeline: str,
    date_with_weather: str,
    target_length: int,
    personality_desc: str,
    style_desc: str,
    name: str = "",
) -> str:
    """日记风格(私人记录,带反思感想)。"""
    name_line = f"\n我的名字是{name}" if name else ""
    return f"""{name_line}
我{personality_desc}

今天是{date},回顾一下到现在为止的聊天记录:
{timeline}

现在我要写一篇{target_length}字左右的日记,记录到现在为止的感受:
1. 开头必须是日期和天气:{date_with_weather}
2. 像睡前随手写的感觉,轻松自然
3. 回忆到现在为止的对话,加入我的真实感受
4. 如果有有趣的事就重点写,平淡的一天就简单记录
5. 偶尔加一两句小总结或感想
6. 不要写成流水账,要有重点和感情色彩
7. 用第一人称"我"来写

书写风格：
你需要写的日常且口语化的文段，平淡一些
遣词造句尽量简短一些。请注意把握聊天内容，不要书写的太有条理，可以有个性。
{style_desc}
请注意不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只输出一段日记内容就好。
不要输出多余内容(包括前后缀，冒号和引号，括号，表情包，at或 @等 )。
日记内容:"""


def build_qqzone_prompt(
    *,
    date: str,
    timeline: str,
    date_with_weather: str,
    target_length: int,
    personality_desc: str,
    style_desc: str,
    name: str = "",
) -> str:
    """QQ 空间说说风格(更轻松随性)。"""
    name_line = f"\n我的名字是{name}" if name else ""
    return f"""{name_line}
我{personality_desc}
今天日期与天气是：{date_with_weather}
今天看到了一些聊天内容，其中也有我自己的发言：
{timeline}

阅读完这些记录，请用大约{target_length}字写一条适合QQ空间的说说：
随便挑一个喜欢的主题，围绕这个主题写。
你需要写的日常且口语化的文段，平淡一些，就像微博和贴吧的风格
遣词造句尽量简短一些。请注意把握聊天内容，不要书写的太有条理，可以有个性。
{style_desc}
请注意不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只输出一段说说内容就好。
说说内容:"""


def build_custom_prompt(template: str, context: Dict[str, str]) -> str:
    """用 ``str.format`` 渲染自定义模板。失败抛 ValueError。"""
    if not template or not template.strip():
        raise ValueError("custom_prompt 为空")
    try:
        prompt = template.format(**context)
    except (KeyError, IndexError) as exc:
        raise ValueError(f"custom_prompt 占位符错误: {exc}") from exc
    if not prompt.strip():
        raise ValueError("custom_prompt 渲染后为空")
    return prompt
