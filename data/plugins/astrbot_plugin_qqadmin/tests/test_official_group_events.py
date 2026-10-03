"""群成员事件注入测试：qq-botpy 解析器、intent 位与客户端处理器

覆盖 core/official_group_events.py 的三层注入，确保 AstrBot 核心无需改动
即可收到入群申请、成员进退群、机器人进退群事件。
"""

import asyncio

import botpy
import pytest
from astrbot.api.platform import MessageType
from botpy.connection import ConnectionState

from astrbot_plugin_qqadmin.core.official_group_events import (
    GROUP_EVENT_NAMES,
    GROUP_MEMBER_INTENT,
    QQGroupEvent,
    _build_message,
    install,
    is_installed,
)

GROUP_OPENID = "30584554AA2BF4E72BD3B8F27A70339D"
MEMBER_OPENID = "FE003FAF76C4817251FDC128A16753BB"

EVENT_NAMES = {
    "group_join_request": "GROUP_JOIN_REQUEST",
    "group_member_add": "GROUP_MEMBER_ADD",
    "group_member_remove": "GROUP_MEMBER_REMOVE",
    "group_add_robot": "GROUP_ADD_ROBOT",
    "group_del_robot": "GROUP_DEL_ROBOT",
}


@pytest.fixture(autouse=True)
def ensure_injection():
    """注入必须处于激活状态，且重复调用应保持幂等"""
    assert install() is True
    assert install() is True
    assert is_installed() is True


@pytest.fixture
def current_event_loop():
    """botpy.Client 构造时会取当前事件循环，测试环境下需要先兜底设置"""
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


class FakePlatform:
    """记录会话场景的适配器替身"""

    def __init__(self) -> None:
        self.scenes: list[tuple[str, str]] = []

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        self.scenes.append((session_id, scene))


class RecordingClient(botpy.Client):
    """记录已提交 AstrBot 消息的 botpy 客户端"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.platform = FakePlatform()
        self.committed: list = []

    def _commit(self, abm) -> None:
        self.committed.append(abm)


def build_client(**kwargs) -> RecordingClient:
    intents = botpy.Intents(public_messages=True, public_guild_messages=True)
    return RecordingClient(intents=intents, bot_log=False, **kwargs)


def build_state(dispatched: list) -> ConnectionState:
    return ConnectionState(
        dispatch=lambda event_name, *args: dispatched.append((event_name, args)),
        api=None,
    )


def test_parsers_registered_on_connection_state():
    state = build_state([])

    for parser_name in GROUP_EVENT_NAMES:
        assert parser_name in state.parsers


@pytest.mark.parametrize(("parser_name", "event_name"), sorted(EVENT_NAMES.items()))
def test_parser_dispatches_raw_payload(parser_name: str, event_name: str):
    dispatched = []
    state = build_state(dispatched)
    payload = {
        "t": event_name,
        "d": {"group_openid": GROUP_OPENID, "member_openid": MEMBER_OPENID},
    }

    state.parsers[parser_name](payload)

    (dispatch_name, args), = dispatched
    assert dispatch_name == parser_name
    (event,) = args
    assert isinstance(event, QQGroupEvent)
    assert event.event_name == event_name
    assert event.raw_data["group_openid"] == GROUP_OPENID


def test_parser_falls_back_to_event_name_without_t(current_event_loop):
    dispatched = []
    state = build_state(dispatched)

    state.parsers["group_add_robot"]({"d": {}})

    (_, args), = dispatched
    (event,) = args
    assert event.event_name == "group_add_robot"


def test_intent_bit_is_forced_on_current_event_loop():
    intents = botpy.Intents(public_messages=True, public_guild_messages=True)

    client = RecordingClient(intents=intents, bot_log=False)

    assert intents.value & GROUP_MEMBER_INTENT == GROUP_MEMBER_INTENT
    # qq-botpy 用 int 副本建立握手，副本也必须带上该位
    assert client.intents & GROUP_MEMBER_INTENT == GROUP_MEMBER_INTENT


def test_intent_bit_does_not_drop_existing_flags(current_event_loop):
    client = build_client()

    assert client.intents & (1 << 25)  # 群消息位保持开启
    assert client.intents & GROUP_MEMBER_INTENT == GROUP_MEMBER_INTENT


def test_handlers_installed_on_client_class(current_event_loop):
    build_client()

    for event_name in GROUP_EVENT_NAMES:
        assert hasattr(RecordingClient, f"on_{event_name}")


@pytest.mark.parametrize(("parser_name", "event_name"), sorted(EVENT_NAMES.items()))
def test_handler_commits_group_message(
    parser_name: str, event_name: str, current_event_loop
):
    client = build_client()
    event = QQGroupEvent(
        {
            "group_openid": GROUP_OPENID,
            "member_openid": MEMBER_OPENID,
            "username": "小明",
        },
        event_name,
    )

    asyncio.run(getattr(client, f"on_{parser_name}")(event))

    (abm,) = client.committed
    assert abm.type == MessageType.GROUP_MESSAGE
    assert abm.group_id == GROUP_OPENID
    assert abm.session_id == GROUP_OPENID
    assert abm.self_id == "qq_official"
    assert abm.sender.user_id == MEMBER_OPENID
    assert abm.sender.nickname == "小明"
    assert abm.message_str == ""
    assert abm.raw_message is event
    assert client.platform.scenes == [(GROUP_OPENID, "group")]


def test_handler_skips_session_scene_without_group_id(current_event_loop):
    client = build_client()
    event = QQGroupEvent({}, "GROUP_ADD_ROBOT")

    asyncio.run(client.on_group_add_robot(event))

    (abm,) = client.committed
    assert abm.group_id == ""
    assert abm.sender.user_id == ""
    assert client.platform.scenes == []


def test_build_message_reads_nested_author_openid():
    event = QQGroupEvent(
        {"author": {"member_openid": MEMBER_OPENID}, "group_openid": GROUP_OPENID},
        "GROUP_MEMBER_REMOVE",
    )

    abm = _build_message(event)

    assert abm.sender.user_id == MEMBER_OPENID
    assert abm.group_id == GROUP_OPENID


def test_event_matches_consumer_interface():
    """插件消费端读取 raw_data 与 event_name，注入对象必须提供这两个属性"""
    raw = QQGroupEvent({"group_openid": GROUP_OPENID}, "GROUP_MEMBER_ADD")

    assert isinstance(getattr(raw, "raw_data", None), dict)
    assert str(getattr(raw, "event_name", "")) == "GROUP_MEMBER_ADD"


def test_event_tolerates_non_dict_payload():
    event = QQGroupEvent("not-a-dict", None)

    assert event.raw_data == {}
    assert event.event_name == ""
