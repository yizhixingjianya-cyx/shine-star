"""每日鉴毛（Furry Will / Furry Gallery）API 客户端。

对接 https://fur-bot.com/zh/blog-detail.html?post=mrjm 文档中的接口：
随机抽卡 / 期数查询 / 名称搜索 / 地区搜索 / 工作室搜索 / 健康检查。
由 cyxbot 的 ``pyfurbot/command/furry_will.py`` 移植，去掉 nonebot 依赖。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt

from .config import FurryWillSettings

logger = logging.getLogger("astrbot")

__all__ = ["FurryWillError", "FurryWillAPI"]

API_PREFIX = "/furry_will"


class FurryWillError(Exception):
    """业务/系统错误，message 可直接展示给用户。"""


def _generate_jwt(qq: str, secret_key: str) -> str:
    """按文档生成 HS256 JWT，token 有效期 120 秒，须每次新生成。"""
    payload = {"qq": qq, "timestamp": int(time.time())}
    return jwt.encode(payload, secret_key, algorithm="HS256")


class FurryWillAPI:
    """每日鉴毛 API 客户端。"""

    def __init__(self, settings: FurryWillSettings) -> None:
        self.settings = settings
        self._timeout = 30.0

    async def request(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """发起带认证的 POST，统一处理 HTTP 非 200 与业务错误。

        Args:
            path: API 路径。
            params: 查询参数。

        Returns:
            响应 JSON。

        Raises:
            FurryWillError: 业务或系统错误。
        """
        if not self.settings.configured():
            raise FurryWillError(
                "尚未配置每日鉴毛 API 凭证，请填写 furry_will_base_url / "
                "furry_will_jwt_secret / furry_will_jwt_qq"
            )
        token = _generate_jwt(self.settings.jwt_qq, self.settings.jwt_secret)
        url = f"{self.settings.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url,
                    params=params,
                    json={"qq": self.settings.jwt_qq, "token": token},
                )
                body = resp.json()
        except httpx.RequestError as e:
            raise FurryWillError(f"网络连接失败：{e}") from e
        except ValueError as e:
            raise FurryWillError(f"响应解析失败：{e}") from e

        if resp.status_code != 200:
            raise FurryWillError(
                f"[HTTP {resp.status_code}] {body.get('message', '访问受限/服务异常')}"
            )
        if body.get("status") != "success":
            raise FurryWillError(body.get("message", "业务错误"))
        return body

    async def random(self) -> dict:
        """随机抽一张。"""
        return await self.request(f"{API_PREFIX}/random")

    async def by_qishu(self, qishu: str) -> dict:
        """按期刊号查询（随机一张）。"""
        return await self.request(f"{API_PREFIX}/qishu", {"qishu": qishu, "all": "0"})

    async def by_name(self, name: str) -> dict:
        """按名称搜索（随机一张）。"""
        return await self.request(f"{API_PREFIX}/name", {"name": name, "all": "0"})

    async def by_city(self, city: str) -> dict:
        """按地区搜索。"""
        return await self.request(
            f"{API_PREFIX}/city", {"city": city, "model": "random", "count": "1"}
        )

    async def by_studio(self, studio: str) -> dict:
        """按工作室搜索。"""
        return await self.request(
            f"{API_PREFIX}/studio", {"studio": studio, "model": "random", "count": "1"}
        )

    async def health(self) -> tuple[int, dict]:
        """健康检查（无需认证）。"""
        url = f"{self.settings.base_url}/health"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                body = resp.json()
        except httpx.RequestError as e:
            raise FurryWillError(f"无法连接 {self.settings.base_url}（{e}）") from e
        return resp.status_code, body
