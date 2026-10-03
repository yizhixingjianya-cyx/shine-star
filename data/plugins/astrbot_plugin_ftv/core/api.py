"""FTV / FurryWill 平台 API 客户端（由 cyxbot ftv 插件移植）。

带令牌换取/刷新的轻量客户端，仅依赖 ``httpx``；凭据由插件配置注入
（``ftv_base_url`` / ``ftv_app_id`` / ``ftv_client_secret``）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger("astrbot")

__all__ = ["FtvError", "FtvAPI"]

_TOKEN_ENDPOINT = "/api/auth/token"
_TIMEOUT = 30.0


class FtvError(Exception):
    """带中文说明的业务/网络错误。"""


class FtvAPI:
    """FTV API 客户端，负责令牌换取/刷新与 GET 请求。"""

    def __init__(
        self, base_url: str = "", app_id: str = "", client_secret: str = ""
    ) -> None:
        self.base_url = (base_url or "https://open-cn1.vdsentnet.com").rstrip("/")
        self.app_id = (app_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self._api_key: str | None = None
        self._access_token: str | None = None
        self._grants: list[str] = []
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    def configured(self) -> bool:
        """是否已配置应用凭据。"""
        return bool(self.app_id and self.client_secret)

    # ------------------------------------------------------------ 令牌
    async def _ensure_token(self) -> None:
        if self._api_key and self._expires_at and self._expires_at > datetime.now():
            return
        if not self.configured():
            raise FtvError(
                "未在插件配置中填写 ftv_app_id / ftv_client_secret，"
                "请填写后保存配置并重载插件。"
            )
        async with self._lock:
            if self._api_key and self._expires_at and self._expires_at > datetime.now():
                return
            url = f"{self.base_url}{_TOKEN_ENDPOINT}"
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        url,
                        json={"appId": self.app_id, "clientSecret": self.client_secret},
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError as e:
                raise FtvError(
                    f"换取令牌失败 HTTP {e.response.status_code}: {e.response.text[:200]}"
                )
            except httpx.RequestError as e:
                raise FtvError(f"换取令牌网络错误: {e}")
            self._api_key = data.get("apiKey")
            self._access_token = data.get("accessToken")
            self._grants = list(data.get("grants") or [])
            seconds = int(data.get("expiresInSeconds") or 3600)
            self._expires_at = datetime.now() + timedelta(seconds=seconds - 60)
            if not self._api_key:
                raise FtvError(f"换取令牌响应缺少 apiKey: {str(data)[:200]}")

    def _has_grant(self, grant: str) -> bool:
        if not grant:
            return True
        if grant in self._grants:
            return True
        for g in self._grants:
            if g == grant.split(".")[0] or g.startswith(grant + "."):
                return True
        return False

    # ------------------------------------------------------------ 请求
    async def request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        required_grant: str | None = None,
    ) -> dict[str, Any]:
        """GET 一个端点，返回 JSON；权限不足/404 抛对应异常。

        Args:
            endpoint: API 端点路径。
            params: 查询参数。
            required_grant: 需要的权限组。

        Returns:
            响应 JSON。

        Raises:
            PermissionError: 凭据缺少所需权限。
            FileNotFoundError: 端点返回 404。
            FtvError: 其它网络/业务错误。
        """
        await self._ensure_token()
        if required_grant and not self._has_grant(required_grant):
            raise PermissionError(
                f"权限不足：{required_grant}（当前权限：{'、'.join(self._grants) or '无'}）"
            )
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.base_url}{endpoint}",
                    headers={
                        "X-Api-Key": self._api_key or "",
                        "Content-Type": "application/json",
                    },
                    params=params,
                )
        except httpx.RequestError as e:
            raise FtvError(f"网络请求失败: {e}") from e

        if resp.status_code == 404:
            raise FileNotFoundError(f"资源未找到：{endpoint}")
        if resp.status_code in (401, 403):
            raise FtvError(f"接口返回 {resp.status_code}：凭据无效或无访问权限")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise FtvError(
                f"请求失败 HTTP {e.response.status_code}: {e.response.text[:200]}"
            ) from e
        return resp.json()

    async def get_access_token(self) -> str | None:
        """获取临时 accessToken（用于下载展示图，自动刷新）。"""
        await self._ensure_token()
        return self._access_token

    def auth_headers(self) -> dict[str, str]:
        """返回带 apiKey 的认证请求头。"""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        return headers

    def download_headers(self) -> dict[str, str]:
        """返回下载展示图用的浏览器请求头（含 accessToken）。"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if self._access_token:
            headers["x-api-Token"] = self._access_token
        return headers

    # ---------------------------------------------------- 发现与搜索
    async def get_popular(self, limit: int = 10) -> dict:
        """获取热门推荐档案（不缓存）。"""
        return await self.request(
            "/api/proxy/furtv/popular", {"limit": limit}, "furtv.discovery"
        )

    async def get_random(self, count: int = 1) -> dict:
        """获取随机推荐档案（不缓存）。"""
        return await self.request(
            "/api/proxy/furtv/fursuit/random", {"count": count}, "furtv.fursuit"
        )

    async def search_by_species(
        self, species: str, page: int = 1, limit: int = 20
    ) -> dict:
        """按物种搜索公开档案。"""
        return await self.request(
            f"/api/proxy/furtv/search/species/{species}",
            {"page": page, "limit": limit},
            "furtv.discovery",
        )

    async def search(
        self, q: str, type: str = "all", page: int = 1, limit: int = 20
    ) -> dict:
        """按关键词搜索公开档案。"""
        return await self.request(
            "/api/proxy/furtv/search",
            {"q": q, "type": type, "page": page, "limit": limit},
            "furtv.discovery",
        )

    async def get_popular_locations(self) -> dict:
        """获取热门地区统计。"""
        return await self.request(
            "/api/proxy/furtv/locations/popular", None, "furtv.discovery"
        )

    async def get_species_list(self) -> dict:
        """获取物种统计列表。"""
        return await self.request("/api/proxy/furtv/species", None, "furtv.discovery")

    async def get_search_suggestions(self, q: str) -> dict:
        """获取搜索自动补全建议（q 至少 2 个字符）。"""
        return await self.request(
            "/api/proxy/furtv/search/suggestions", {"q": q}, "furtv.discovery"
        )

    async def get_platform_health(self) -> dict:
        """检查平台 API 健康状态。"""
        return await self.request("/api/proxy/furtv/health", None, "furtv.health")

    # ---------------------------------------------------- 学校与角色
    async def search_schools(self, query: str) -> dict:
        """按名称搜索学校。"""
        return await self.request(
            "/api/proxy/furtv/schools/search", {"query": query}, "furtv.schools"
        )

    async def get_school_detail(self, school_id: str) -> dict:
        """获取学校详情。"""
        return await self.request(
            f"/api/proxy/furtv/schools/{school_id}", None, "furtv.schools"
        )

    async def get_user_characters(self, username: str) -> dict:
        """获取用户角色列表。"""
        return await self.request(
            f"/api/proxy/furtv/characters/user/{username}", None, "furtv.characters"
        )

    # ---------------------------------------------------- 用户公开资料
    async def get_user_profile(self, username: str) -> dict:
        """获取用户公开资料。"""
        return await self.request(
            f"/api/proxy/furtv/users/{username}", None, "furtv.users"
        )

    async def get_user_info_by_id(self, user_id: str) -> dict:
        """通过用户 ID 获取公开基础资料。"""
        return await self.request(
            f"/api/proxy/furtv/users/id/{user_id}", None, "furtv.users"
        )

    async def get_user_like_status(self, username: str) -> dict:
        """查询用户点赞状态。"""
        return await self.request(
            f"/api/proxy/furtv/fursuit/like-status/{username}", None, "furtv.fursuit"
        )

    # ---------------------------------------------------- Today 公开内容
    async def get_today_explore(
        self, limit: int = 10, exclude_today_id: str | None = None
    ) -> dict:
        """Today 探索流（实时）。"""
        params = {"limit": limit}
        if exclude_today_id:
            params["exclude_today_id"] = exclude_today_id
        return await self.request(
            "/api/proxy/furtv/today/explore", params, "furtv.today"
        )

    async def get_today_feed(
        self, limit: int = 10, scope: str | None = None, date: str | None = None
    ) -> dict:
        """Today 信息流（实时）。"""
        params: dict[str, Any] = {"limit": limit}
        if scope:
            params["scope"] = scope
        if date:
            params["date"] = date
        return await self.request("/api/proxy/furtv/today/feed", params, "furtv.today")

    async def get_today_detail(
        self, today_id: str, explore_limit: int | None = None
    ) -> dict:
        """Today 详情。"""
        params = {"explore_limit": explore_limit} if explore_limit else None
        return await self.request(
            f"/api/proxy/furtv/today/{today_id}", params, "furtv.today"
        )

    async def get_today_topics(self, topic_identifier: str, limit: int = 10) -> dict:
        """话题下的 Today 动态。"""
        return await self.request(
            f"/api/proxy/furtv/today/topics/{topic_identifier}",
            {"limit": limit},
            "furtv.today",
        )

    async def get_today_gatherings(self, gathering_id: str, limit: int = 10) -> dict:
        """聚会关联的 Today 动态。"""
        return await self.request(
            f"/api/proxy/furtv/today/gatherings/{gathering_id}",
            {"limit": limit},
            "furtv.today",
        )

    async def get_today_user_timeline(
        self, user_identifier: str, limit: int = 10
    ) -> dict:
        """某用户的 Today 动态时间线。"""
        return await self.request(
            f"/api/proxy/furtv/today/users/{user_identifier}",
            {"limit": limit},
            "furtv.today",
        )

    async def get_today_user_current(
        self, user_identifier: str, explore_limit: int | None = None
    ) -> dict:
        """某用户当前(今日)的 Today 动态。"""
        params = {"explore_limit": explore_limit} if explore_limit else None
        return await self.request(
            f"/api/proxy/furtv/today/users/{user_identifier}/current",
            params,
            "furtv.today",
        )

    # ---------------------------------------------------- 聚会
    async def get_gatherings_yearly_stats(self) -> dict:
        """获取当前年份聚会总数。"""
        return await self.request(
            "/api/proxy/furtv/gatherings/stats/this-year", None, "furtv.gatherings"
        )

    async def get_gatherings_monthly(self, year: int, month: int) -> dict:
        """按月份返回聚会列表。"""
        return await self.request(
            "/api/proxy/furtv/gatherings/monthly",
            {"year": year, "month": month},
            "furtv.gatherings",
        )

    async def get_gathering_detail(self, gathering_id: str) -> dict:
        """获取聚会详情。"""
        return await self.request(
            f"/api/proxy/furtv/gatherings/{gathering_id}", None, "furtv.gatherings"
        )
