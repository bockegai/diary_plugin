"""QQ 空间日记发布器。

通过 Napcat HTTP API 拉 QQ 空间 cookies,然后调
``emotion_cgi_publish_v6`` 发说说。

UIN 由调用方通过 ``await ctx.config.get("bot.qq_account", 0)`` 取得后
传入(原 legacy 是 storage.py 自己读 config_api,本期解耦)。
"""

import json
import logging
import os
from typing import Dict

import httpx

logger = logging.getLogger(__name__)


class QzonePublisher:
    """QQ 空间发布器。"""

    def __init__(self, uin: int, plugin_dir: str = "") -> None:
        self.uin = int(uin) if uin else 0
        if self.uin <= 0:
            logger.warning("QQ 账号无效(%s),发布会失败", self.uin)
        self.cookies: Dict[str, str] = {}
        self.gtk2 = ""

        if plugin_dir:
            base = os.path.abspath(plugin_dir)
        else:
            base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        safe_uin = max(self.uin, 0)
        self.cookie_file = os.path.join(base, "data", f"qzone_cookies_{safe_uin}.json")

    async def publish(
        self,
        content: str,
        napcat_host: str = "127.0.0.1",
        napcat_port: str = "9998",
        napcat_token: str = "",
    ) -> bool:
        """发布日记到 QQ 空间。返回是否成功。"""
        if self.uin <= 0:
            logger.error("uin 未配置,无法发布 QQ 空间")
            return False

        if not await self._renew_cookies(napcat_host, napcat_port, napcat_token):
            logger.error("无法获取 QQ 空间 cookies")
            return False

        if not self.cookies or not self.gtk2:
            logger.error("QQ 空间 cookies 无效")
            return False

        publish_url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/"
            "emotion_cgi_publish_v6"
        )
        post_data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    publish_url,
                    params={"g_tk": self.gtk2, "uin": self.uin},
                    data=post_data,
                    headers={
                        "referer": f"https://user.qzone.qq.com/{self.uin}",
                        "origin": "https://user.qzone.qq.com",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    },
                    cookies=self.cookies,
                )
            if resp.status_code != 200:
                logger.error("QQ 空间 API 返回 %s", resp.status_code)
                return False
            result = resp.json()
            if "tid" in result:
                return True
            logger.error("QQ 空间发布失败: %s", result)
            return False
        except Exception as exc:
            logger.error("发布 QQ 空间失败: %s", exc, exc_info=True)
            return False

    async def _renew_cookies(self, host: str, port: str, token: str) -> bool:
        """通过 Napcat 拉新 cookies 写到 cookie_file。失败则尝试加载旧文件。"""
        try:
            cookie_dict = await self._fetch_cookies_via_napcat(host, port, token)
            os.makedirs(os.path.dirname(self.cookie_file), exist_ok=True)
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, ensure_ascii=False, indent=4)
            self.cookies = cookie_dict
            if "p_skey" in cookie_dict:
                self.gtk2 = self._gen_gtk(cookie_dict["p_skey"])
            return True
        except Exception as exc:
            logger.error("Napcat 取 cookies 失败: %s", exc)
            # fallback: 加载本地缓存
            try:
                if os.path.exists(self.cookie_file):
                    with open(self.cookie_file, "r", encoding="utf-8") as f:
                        self.cookies = json.load(f)
                    if "p_skey" in self.cookies:
                        self.gtk2 = self._gen_gtk(self.cookies["p_skey"])
                    logger.info("使用本地 cookies 文件")
                    return True
            except Exception as load_exc:
                logger.error("加载本地 cookies 失败: %s", load_exc)
            return False

    @staticmethod
    async def _fetch_cookies_via_napcat(host: str, port: str, token: str) -> Dict[str, str]:
        """调 Napcat /get_cookies 取 user.qzone.qq.com 的 cookies。"""
        url = f"http://{host}:{port}/get_cookies"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json={"domain": "user.qzone.qq.com"}, headers=headers)
            resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok" or "cookies" not in data.get("data", {}):
            raise RuntimeError(f"获取 cookie 失败: {data}")
        cookie_str = data["data"]["cookies"]
        cookies: Dict[str, str] = {}
        for pair in cookie_str.split("; "):
            if "=" in pair:
                key, value = pair.split("=", 1)
                cookies[key] = value
        return cookies

    @staticmethod
    def _gen_gtk(skey: str) -> str:
        """根据 p_skey 生成 g_tk。"""
        hash_val = 5381
        for ch in skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 2147483647)
