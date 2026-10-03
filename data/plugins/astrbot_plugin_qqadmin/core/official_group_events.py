"""Runtime injection of QQ group member event support into qq-botpy.

AstrBot's QQ official adapter registers parsers for message events only. Group
member events (join requests, member join/leave, bot join/leave) are dropped by
qq-botpy before reaching AstrBot, and qq-botpy exposes no intent flag for them.

Patching the AstrBot core tree works but has to be redone after every AstrBot
upgrade, so the support is injected from this plugin instead. Three pieces are
installed on qq-botpy:

1. ``parse_*`` parsers on ``ConnectionState``, so both the websocket and the
   webhook transport route the events. ``ConnectionState`` snapshots its
   parsers at construction time, so they are registered before any client
   builds its connection session.
2. The ``1 << 24`` intent bit. ``botpy.Client.__init__`` copies
   ``Intents.value`` into a plain int, so the bit must be set on the intents
   object before the original constructor runs.
3. ``on_*`` handlers on the concrete client class. qq-botpy resolves handlers
   with ``getattr`` on the client instance at dispatch time, so they only need
   to exist before the first event arrives.

Installing at import time is early enough: AstrBot loads plugins before it
instantiates platform adapters.
"""

import functools
import time
from typing import Any

from astrbot import logger
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

# qq-botpy has no switch for the group member event intent bit.
GROUP_MEMBER_INTENT = 1 << 24

# Lower-case qq-botpy dispatch names, matching the parser and handler suffixes.
GROUP_EVENT_NAMES = (
    "group_join_request",
    "group_member_add",
    "group_member_remove",
    "group_add_robot",
    "group_del_robot",
)

_installed = False


class QQGroupEvent:
    """Group member event payload.

    qq-botpy provides no model for these events, so only the event name and the
    raw body are kept.

    Args:
        data: Raw event body from QQ.
        event_name: Upper-case event name, e.g. ``GROUP_MEMBER_ADD``.
    """

    __slots__ = ("id", "event_name", "raw_data")

    def __init__(self, data: Any, event_name: str | None = None) -> None:
        self.raw_data = data if isinstance(data, dict) else {}
        self.event_name = event_name or ""
        self.id = self.raw_data.get("id")


def _build_message(event: QQGroupEvent) -> AstrBotMessage:
    """Convert a group member event into an AstrBot message.

    The text body stays empty on purpose: consumers read ``raw_message`` for
    the event name and the raw body.

    Args:
        event: Group member event object.

    Returns:
        AstrBot message carrying the event as a group message.
    """
    data: dict[str, Any] = getattr(event, "raw_data", None) or {}
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    member_openid = str(
        data.get("member_openid") or (author or {}).get("member_openid") or ""
    )

    abm = AstrBotMessage()
    abm.type = MessageType.GROUP_MESSAGE
    abm.timestamp = int(time.time())
    abm.raw_message = event
    abm.message_id = str(data.get("id") or "")
    abm.group_id = str(data.get("group_openid") or "")
    abm.session_id = abm.group_id
    abm.self_id = "qq_official"
    abm.sender = MessageMember(member_openid, str(data.get("username") or ""))
    abm.message = []
    abm.message_str = ""
    return abm


def _make_handler(event_name: str) -> Any:
    """Build the client handler for one group member event.

    Args:
        event_name: Lower-case qq-botpy dispatch event name.

    Returns:
        Coroutine function usable as an ``on_<event_name>`` client method.
    """

    async def on_group_event(self, event: QQGroupEvent) -> None:
        abm = _build_message(event)
        if abm.group_id:
            self.platform.remember_session_scene(abm.session_id, "group")
        self._commit(abm)

    on_group_event.__name__ = f"on_{event_name}"
    on_group_event.__qualname__ = f"on_{event_name}"
    return on_group_event


def _make_parser(event_name: str) -> Any:
    """Build the ConnectionState parser for one group member event.

    Args:
        event_name: Lower-case qq-botpy dispatch event name.

    Returns:
        Parser callable bound by qq-botpy's ConnectionState.
    """

    def parse_group_event(self, payload: dict[str, Any]) -> None:
        event = QQGroupEvent(payload.get("d", {}), payload.get("t") or event_name)
        self._dispatch(event_name, event)

    return parse_group_event


def _register_parsers() -> None:
    """Register the group event parsers on qq-botpy's ConnectionState."""
    from botpy.connection import ConnectionState

    for event_name in GROUP_EVENT_NAMES:
        setattr(ConnectionState, f"parse_{event_name}", _make_parser(event_name))


def _install_handlers(client_cls: type) -> None:
    """Attach the group event handlers to a client class.

    Args:
        client_cls: Concrete ``botpy.Client`` subclass to extend.
    """
    for event_name in GROUP_EVENT_NAMES:
        method = f"on_{event_name}"
        if not hasattr(client_cls, method):
            setattr(client_cls, method, _make_handler(event_name))


def install() -> bool:
    """Inject group member event support into qq-botpy.

    Idempotent: repeated calls leave the patches in place. Only the first call
    wraps ``botpy.Client.__init__``.

    Returns:
        True when the injection is active, False when qq-botpy is unavailable.
    """
    global _installed
    if _installed:
        return True

    try:
        import botpy
    except ImportError:
        logger.warning("[群成员事件] 未安装 qq-botpy，无法注入群成员事件支持")
        return False

    _register_parsers()

    original_init = botpy.Client.__init__

    @functools.wraps(original_init)
    def patched_init(self, intents: Any = None, *args: Any, **kwargs: Any) -> None:
        # Parsers are snapshotted when the connection session is built.
        _register_parsers()
        if intents is not None:
            # qq-botpy copies Intents.value in Client.__init__, so the bit has
            # to be set before the original constructor runs.
            intents.value |= GROUP_MEMBER_INTENT
        original_init(self, intents, *args, **kwargs)
        _install_handlers(type(self))

    botpy.Client.__init__ = patched_init
    _installed = True
    logger.info(
        "[群成员事件] 已通过 QQ群管 注入 qq-botpy：入群申请、成员进退群、机器人进退群 实时生效"
    )
    return True


def is_installed() -> bool:
    """Return whether the injection is active.

    Returns:
        True when the qq-botpy patches are in place.
    """
    return _installed
