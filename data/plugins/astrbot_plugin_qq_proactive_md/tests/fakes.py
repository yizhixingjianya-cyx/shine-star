"""联调用的假 AstrBot / QQ 运行时对象。

只模拟工具层真正接触到的接口：平台实例、平台元数据、qq-botpy 的 BotAPI、
消息事件与 Agent 上下文。所有发出的 QQ 调用都会被记录，便于断言请求载荷。
"""

from __future__ import annotations

from typing import Any


class FakeBotApi:
    """qq-botpy ``BotAPI`` 的最小实现，记录调用参数并可按需抛错。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raises: dict[str, list[BaseException]] = {}

    def _record(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        pending = self.raises.get(method)
        if pending:
            raise pending.pop(0)
        return {"id": f"msg_{len(self.calls)}", "timestamp": 1700000000}

    def fail_next(self, method: str, exc: BaseException) -> None:
        """让下一次指定方法的调用抛出异常。

        Args:
            method: 方法名，例如 ``post_group_message``。
            exc: 要抛出的异常。
        """
        self.raises.setdefault(method, []).append(exc)

    async def post_group_message(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_group_message", kwargs)

    async def post_c2c_message(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_c2c_message", kwargs)

    async def post_message(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_message", kwargs)

    async def post_dms(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_dms", kwargs)

    def last_call(self) -> tuple[str, dict[str, Any]]:
        """返回最后一次调用。"""
        return self.calls[-1]


class FakePlatformMetadata:
    """平台元数据。"""

    def __init__(
        self,
        name: str = "qq_official",
        platform_id: str = "qq_main",
        support_proactive_message: bool = True,
    ) -> None:
        self.name = name
        self.id = platform_id
        self.description = "fake"
        self.support_proactive_message = support_proactive_message


class FakeClient:
    """qq-botpy ``Client`` 的最小实现。"""

    def __init__(self, api: FakeBotApi) -> None:
        self.api = api


class FakePlatform:
    """AstrBot 平台适配器实例的最小实现。"""

    def __init__(
        self,
        name: str = "qq_official",
        platform_id: str = "qq_main",
        support_proactive_message: bool = True,
    ) -> None:
        self.api = FakeBotApi()
        self._meta = FakePlatformMetadata(name, platform_id, support_proactive_message)

    def meta(self) -> FakePlatformMetadata:
        return self._meta

    def get_client(self) -> FakeClient:
        return FakeClient(self.api)


class RawGroupMessage:
    """模拟 botpy.message.GroupMessage 的关键属性。"""

    def __init__(self) -> None:
        self.group_openid = "B1A2C3D4E5"
        self.author = object()


class RawC2CMessage:
    """模拟 botpy.message.C2CMessage 的关键属性。"""

    def __init__(self) -> None:
        self.author = object()


class RawChannelMessage:
    """模拟 botpy.message.Message（文字子频道）的关键属性。"""

    def __init__(self) -> None:
        self.channel_id = "CHANNEL_ID"
        self.guild_id = "GUILD_ID"


class RawDirectMessage:
    """模拟 botpy.message.DirectMessage（频道私信）的关键属性。"""

    def __init__(self) -> None:
        self.guild_id = "GUILD_ID"


class FakeMessageObj:
    """AstrBot 消息对象。"""

    def __init__(self, raw_message: Any, message_id: str | None) -> None:
        self.raw_message = raw_message
        self.message_id = message_id


class FakeEvent:
    """AstrBot 消息事件。"""

    def __init__(
        self,
        unified_msg_origin: str = "qq_main:GroupMessage:B1A2C3D4E5",
        raw_message: Any | None = None,
        message_id: str | None = "USER_MSG_ID",
        role: str = "member",
    ) -> None:
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = FakeMessageObj(
            raw_message if raw_message is not None else RawGroupMessage(),
            message_id,
        )
        self.role = role

    def is_admin(self) -> bool:
        return self.role == "admin"


class FakeStarContext:
    """AstrBot Star 上下文（仅用到平台查询）。"""

    def __init__(self, platforms: dict[str, FakePlatform] | None = None) -> None:
        self._platforms = {"qq_main": FakePlatform()} if platforms is None else platforms

    def get_platform_inst(self, platform_id: str) -> FakePlatform | None:
        return self._platforms.get(platform_id)


class FakeAgentContext:
    """AstrBot 的 AstrAgentContext。"""

    def __init__(self, star_context: FakeStarContext, event: FakeEvent) -> None:
        self.context = star_context
        self.event = event


class FakeContextWrapper:
    """AstrBot 的 ContextWrapper[AstrAgentContext]。"""

    def __init__(self, star_context: FakeStarContext, event: FakeEvent) -> None:
        self.context = FakeAgentContext(star_context, event)
        self.messages: list[Any] = []
        self.tool_call_timeout = 120
