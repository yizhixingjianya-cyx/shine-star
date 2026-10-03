"""QQ 官方开放平台 OpenAPI 客户端（用于「指令面板」上传）。

鉴权与请求方式参考官方文档：

- 统一地址：``https://api.bot.qq.com``
- 获取凭证：``POST /app/getAppAccessToken``，body ``{appId, clientSecret}``
  → ``{access_token, expires_in}``
- 调用 OpenAPI：请求头 ``Authorization: QQBot {access_token}``

指令面板（快捷菜单指令）的增删改查走 ``/v2/api/panels`` 系列接口，
每个面板最多 20 个指令元素，指令名为「每个指令项」的字段，desc 为描述。

面板端点可通过常量调整；本客户端对返回结构做了兼容解析，并在失败时
抛出带 HTTP 状态与 err_code 的 :class:`QQApiError`，方便面板直接展示。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger("astrbot")

__all__ = ["QQApiError", "QQOfficialClient"]

_BASE_URL = "https://api.bot.qq.com"
_TOKEN_ENDPOINT = "/app/getAppAccessToken"
# 指令面板相关端点（官方 v2 OpenAPI）
_PANELS_ENDPOINT = "/v2/api/panels"
_TIMEOUT = 30.0
# 单个面板最多 20 个指令元素
MAX_ITEMS_PER_PANEL = 20
# 指令名显示宽度上限（1 汉字算 2）、desc 上限
MAX_NAME_WIDTH = 14
MAX_DESC_WIDTH = 30


class QQApiError(Exception):
    """带中文说明的 QQ OpenAPI 错误。"""


def _display_width(text: str) -> int:
    """按 QQ 口径粗略算显示宽度：全角/汉字算 2，其余算 1。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(text))


def _clip_display(text: Any, limit: int) -> str:
    """按显示宽度截断文本，避免触发平台参数校验。"""
    out, width = [], 0
    for ch in str(text or "").strip():
        w = 2 if ord(ch) > 0x2E7F else 1
        if width + w > limit:
            break
        out.append(ch)
        width += w
    return "".join(out)


class QQOfficialClient:
    """轻量 QQ 官方 OpenAPI 客户端（仅本插件用到的能力）。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        sandbox: bool = False,
        base_url: str = _BASE_URL,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.sandbox = bool(sandbox)
        self.base_url = base_url.rstrip("/")
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    def configured(self) -> bool:
        """凭据是否齐全。"""
        return bool(self.app_id and self.app_secret)

    # ------------------------------------------------------------ 令牌
    async def _ensure_token(self) -> str:
        if (
            self._access_token
            and self._expires_at
            and self._expires_at > datetime.now()
        ):
            return self._access_token
        if not self.configured():
            raise QQApiError("未填写 AppID / AppSecret，请先在面板中填写凭据")

        url = f"{self.base_url}{_TOKEN_ENDPOINT}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"appId": self.app_id, "clientSecret": self.app_secret},
                )
                data = resp.json()
        except httpx.RequestError as e:
            raise QQApiError(f"获取 access_token 网络错误：{e}") from e
        except ValueError as e:
            raise QQApiError(f"获取 access_token 响应解析失败：{e}") from e

        token = data.get("access_token")
        if resp.status_code >= 400 or not token:
            raise QQApiError(
                f"获取 access_token 失败（HTTP {resp.status_code}）："
                f"{data.get('message') or data}"
            )
        self._access_token = str(token)
        expires_in = int(data.get("expires_in") or 7200)
        # 提前 60 秒刷新
        self._expires_at = datetime.now() + timedelta(seconds=max(60, expires_in - 60))
        return self._access_token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """发一次带鉴权的请求，返回 JSON（空包体返回 ``{}``）。"""
        token = await self._ensure_token()
        url = f"{self.base_url}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(token),
                    json=json_body,
                    params=params,
                )
        except httpx.RequestError as e:
            raise QQApiError(f"请求 {endpoint} 网络错误：{e}") from e

        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {"raw": resp.text}

        if resp.status_code >= 400 or data.get("err_code"):
            # err_code=0 视为成功
            if data.get("err_code") in (0, "0", None) and resp.status_code < 400:
                return data
            raise QQApiError(
                f"接口 {endpoint} 失败（HTTP {resp.status_code}，"
                f"err_code={data.get('err_code')}）：{data.get('message') or data}"
            )
        return data

    # ------------------------------------------------------------ 面板
    @staticmethod
    def _panel_id(panel: Any) -> str | None:
        """从面板对象里取 panel_id（兼容多种字段名）。"""
        if isinstance(panel, dict):
            for key in ("panel_id", "command_panel_id", "id"):
                if panel.get(key):
                    return str(panel[key])
        return None

    @staticmethod
    def _panel_items(data: Any) -> list[dict]:
        """从面板详情中取出 items 列表。"""
        if isinstance(data, dict):
            inner = data.get("panel")
            if isinstance(inner, dict) and isinstance(inner.get("items"), list):
                return [i for i in inner["items"] if isinstance(i, dict)]
            if isinstance(data.get("items"), list):
                return [i for i in data["items"] if isinstance(i, dict)]
        return []

    async def list_panels(self, scope: str) -> list[dict]:
        """分页拉取某 scope（group / c2c）下的指令面板。"""
        records: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"scope": scope, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._request("GET", _PANELS_ENDPOINT, params=params)
            raw = resp.get("records") or resp.get("panels") or resp.get("list") or []
            if isinstance(raw, dict):
                raw = raw.get("list") or raw.get("panels") or []
            if isinstance(raw, list):
                records.extend(r for r in raw if isinstance(r, dict))
            nxt = resp.get("next_cursor")
            is_end = resp.get("is_end")
            if not nxt or is_end or len(records) > 500:
                break
            cursor = str(nxt)
        return records

    async def create_panel(
        self, scope: str, items: list[dict], remark: str = ""
    ) -> dict:
        """创建一个指令面板。"""
        body: dict[str, Any] = {"scope": scope, "items": items}
        if remark:
            body["remark"] = remark
        return await self._request("POST", _PANELS_ENDPOINT, json_body=body)

    async def update_panel(
        self, panel_id: str, items: list[dict], remark: str = ""
    ) -> dict:
        """覆盖某个指令面板的内容。"""
        body: dict[str, Any] = {"items": items}
        if remark:
            body["remark"] = remark
        return await self._request(
            "PUT", f"{_PANELS_ENDPOINT}/{panel_id}", json_body=body
        )

    async def delete_panel(self, panel_id: str) -> dict:
        """删除某个指令面板。"""
        return await self._request("DELETE", f"{_PANELS_ENDPOINT}/{panel_id}")

    # ------------------------------------------------------------ 业务封装
    @staticmethod
    def build_items(local: list[dict], scope: str) -> list[dict]:
        """把本地指令清单转成面板元素（过滤屏蔽/超长名称，desc 截断）。

        Args:
            local: 指令清单 ``[{name, panel_name, desc, scope, block}, ...]``。
            scope: 目标场景 ``group`` / ``c2c``。

        Returns:
            面板元素列表 ``[{type:"command", name, desc}, ...]``。
        """
        items: list[dict[str, Any]] = []
        for c in local:
            if c.get("block"):
                continue
            if scope not in (c.get("scope") or []):
                continue
            name = str(c.get("panel_name") or c.get("name") or "").strip()
            if not name or _display_width(name) > MAX_NAME_WIDTH:
                continue
            items.append(
                {
                    "type": "command",
                    "name": name,
                    "desc": _clip_display(c.get("desc") or "", MAX_DESC_WIDTH),
                }
            )
        items.sort(key=lambda i: (len(i["name"]), i["name"]))
        return items

    @staticmethod
    def chunk(items: list[dict], size: int = MAX_ITEMS_PER_PANEL) -> list[list[dict]]:
        """把指令元素按每面板上限分块。"""
        return [items[i : i + size] for i in range(0, len(items), size)] or []

    async def upload_scope(self, scope: str, local: list[dict]) -> dict[str, Any]:
        """对单个 scope 做差分上传：多的删、少的补、变的覆盖。

        Args:
            scope: ``group`` / ``c2c``。
            local: 本地指令清单。

        Returns:
            ``{scope, panels, added, removed}`` 统计。

        Raises:
            QQApiError: 任一步失败。
        """
        chunks = self.chunk(self.build_items(local, scope))
        local_names = {i["name"] for chunk in chunks for i in chunk}

        managed: list[tuple[str, list[dict]]] = []
        for panel in await self.list_panels(scope):
            pid = self._panel_id(panel)
            if pid and not (panel.get("user_openids") or panel.get("group_openids")):
                managed.append((pid, self._panel_items(panel)))

        remote_names = {
            str(i.get("name") or "")
            for _, items in managed
            for i in items
            if i.get("name")
        }
        added = sorted(local_names - remote_names)
        removed = sorted(remote_names - local_names)

        created = 0
        # 1) 多余面板先删
        while len(managed) > len(chunks):
            pid, _ = managed.pop()
            await self.delete_panel(pid)

        # 2) 逐块对齐
        for idx, chunk in enumerate(chunks):
            remark = f"指令上传 {scope} #{idx + 1}/{len(chunks)}"
            if idx < len(managed):
                pid, old_items = managed[idx]
                old_map = {
                    (i.get("name"), i.get("desc"), i.get("type")) for i in old_items
                }
                new_map = {(i["name"], i["desc"], i["type"]) for i in chunk}
                if old_map != new_map or not old_items:
                    await self.update_panel(pid, chunk, remark=remark)
            else:
                await self.create_panel(scope, chunk, remark=remark)
                created += 1

        return {
            "scope": scope,
            "panels": len(managed) + created,
            "added": added,
            "removed": removed,
        }
