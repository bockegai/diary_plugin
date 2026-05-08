"""日记 JSON 文件存储。

数据布局:
- ``data/diaries/YYYY-MM-DD_HHMMSS.json``  单条日记
- ``data/index.json``                       统计索引
"""

import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..utils.date import format_date_str

logger = logging.getLogger(__name__)


class DiaryStorage:
    """JSON 文件存储的日记管理器。"""

    def __init__(self, plugin_dir: str = "") -> None:
        # plugin_dir 为空时落 plugins/diary_plugin/data/(模块路径相对)
        if plugin_dir:
            base = os.path.abspath(plugin_dir)
        else:
            base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.data_dir = os.path.join(base, "data", "diaries")
        self.index_file = os.path.join(base, "data", "index.json")

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)

        if not os.access(self.data_dir, os.W_OK):
            logger.warning("日记数据目录无写入权限: %s", self.data_dir)

    async def save_diary(self, diary_data: Dict[str, Any]) -> bool:
        """保存日记到文件。文件名 ``YYYY-MM-DD_HHMMSS.json``。"""
        try:
            date = diary_data["date"]
            generation_time = diary_data.get("generation_time", time.time())
            timestamp = datetime.datetime.fromtimestamp(generation_time)
            filename = f"{format_date_str(date)}_{timestamp.strftime('%H%M%S')}.json"
            file_path = os.path.join(self.data_dir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(diary_data, f, ensure_ascii=False, indent=2)
            await self._update_index()
            return True
        except Exception as exc:
            logger.error("保存日记失败: %s", exc, exc_info=True)
            return False

    async def get_diary(self, date: str) -> Optional[Dict[str, Any]]:
        """获取指定日期最新一条日记。"""
        diaries = await self.get_diaries_by_date(date)
        if not diaries:
            return None
        return max(diaries, key=lambda d: d.get("generation_time", 0))

    async def get_diaries_by_date(self, date: str) -> List[Dict[str, Any]]:
        """获取指定日期的所有日记,按生成时间升序。"""
        try:
            if not os.path.exists(self.data_dir):
                return []
            prefix = f"{format_date_str(date)}_"
            results: List[Dict[str, Any]] = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(prefix) and filename.endswith(".json"):
                    file_path = os.path.join(self.data_dir, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            results.append(json.load(f))
                    except Exception as exc:
                        logger.warning("读取日记文件 %s 失败: %s", filename, exc)
            results.sort(key=lambda d: d.get("generation_time", 0))
            return results
        except Exception as exc:
            logger.error("读取日期日记失败: %s", exc, exc_info=True)
            return []

    async def list_diaries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """列出最近 ``limit`` 条日记(0 表示不限)。按生成时间降序。"""
        try:
            if not os.path.exists(self.data_dir):
                return []
            results: List[Dict[str, Any]] = []
            for filename in os.listdir(self.data_dir):
                if not filename.endswith(".json"):
                    continue
                file_path = os.path.join(self.data_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        results.append(json.load(f))
                except Exception as exc:
                    logger.warning("读取日记文件 %s 失败: %s", filename, exc)
            results.sort(key=lambda d: d.get("generation_time", 0), reverse=True)
            return results[:limit] if limit > 0 else results
        except Exception as exc:
            logger.error("列出日记失败: %s", exc, exc_info=True)
            return []

    async def get_stats(self) -> Dict[str, Any]:
        """返回总数 / 总字数 / 平均字数 / 最新日期。"""
        try:
            diaries = await self.list_diaries(limit=0)
            if not diaries:
                return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}
            total_count = len(diaries)
            total_words = sum(d.get("word_count", 0) for d in diaries)
            avg_words = total_words // total_count
            latest_date = max(diaries, key=lambda d: d.get("generation_time", 0)).get("date", "无")
            return {
                "total_count": total_count,
                "total_words": total_words,
                "avg_words": avg_words,
                "latest_date": latest_date,
            }
        except Exception as exc:
            logger.error("获取统计失败: %s", exc, exc_info=True)
            return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}

    async def _update_index(self) -> None:
        """重建 index.json(扫描所有日记文件)。"""
        try:
            index_data = {
                "last_update": time.time(),
                "total_diaries": 0,
                "success_count": 0,
                "failed_count": 0,
            }
            if not os.path.exists(self.data_dir):
                with open(self.index_file, "w", encoding="utf-8") as f:
                    json.dump(index_data, f, ensure_ascii=False, indent=2)
                return

            success = 0
            failed = 0
            for filename in os.listdir(self.data_dir):
                if not filename.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.data_dir, filename), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("is_published_qzone", False):
                        success += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

            index_data.update({
                "success_count": success,
                "failed_count": failed,
                "total_diaries": success + failed,
            })
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("更新索引失败: %s", exc, exc_info=True)
