"""日期工具。"""

import datetime
from typing import Any


def format_date_str(date_input: Any) -> str:
    """统一日期格式化为 YYYY-MM-DD。

    支持 datetime 对象 / "YYYY-MM-DD" / "YYYY/M/D" / "YYYY.M.D"。
    解析失败抛 ValueError。
    """
    if isinstance(date_input, datetime.datetime):
        return date_input.strftime("%Y-%m-%d")

    if isinstance(date_input, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.datetime.strptime(date_input, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    raise ValueError(
        f"无法识别的日期格式: {date_input}。支持 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD"
    )


def date_with_weather(date: str, weather: str) -> str:
    """生成 "YYYY年M月D日,星期X,天气。" 的中文日期串。"""
    try:
        date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekdays[date_obj.weekday()]
        return f"{date_obj.year}年{date_obj.month}月{date_obj.day}日,{weekday},{weather}。"
    except Exception:
        return f"{date},{weather}。"
