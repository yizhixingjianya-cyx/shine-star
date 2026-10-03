"""群管 API 适配层测试：官方接口映射、OneBot 兼容与平台判断"""

import asyncio
from datetime import datetime, timedelta

import botpy.errors

import pytest

from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterTypeFilter

from astrbot_plugin_qqadmin.admin_api import (
    OFFICIAL_MAX_BAN_SECONDS,
    OFFICIAL_TZ,
    SUPPORTED_ADAPTER_TYPES,
    SUPPORTED_PLATFORMS,
    AdminAPI,
    OfficialAPIError,
    OfficialUnsupportedError,
    is_official,
    is_supported,
)

from fakes import BOT_ID, GROUP_ID, TARGET_ID, FakeEvent, OfficialBot, OneBotClient

GROUP_NUM = "123456"
USER_NUM = "654321"


def run(coro):
    return asyncio.run(coro)


def official_api(responses: dict | None = None, **kwargs) -> tuple[AdminAPI, FakeEvent]:
    event = FakeEvent(bot=OfficialBot(responses), **kwargs)
    return AdminAPI(event), event


def only_call(event: FakeEvent):
    assert len(event.bot.calls) == 1
    return event.bot.calls[0]


def onebot_api(**kwargs) -> tuple[AdminAPI, FakeEvent, OneBotClient]:
    client = OneBotClient()
    event = FakeEvent(
        platform="aiocqhttp",
        group_id=GROUP_NUM,
        sender_id=USER_NUM,
        bot=client,
        **kwargs,
    )
    return AdminAPI(event), event, client


@pytest.mark.parametrize(
    ("platform", "official"),
    [
        ("qq_official", True),
        ("qq_official_webhook", True),
        ("aiocqhttp", False),
    ],
)
def test_platform_detection(platform: str, official: bool):
    event = FakeEvent(platform=platform, bot=OfficialBot())
    assert is_official(event) is official
    assert is_supported(event) is True


def test_other_platform_is_not_supported():
    event = FakeEvent(platform="telegram", bot=OfficialBot())
    assert is_supported(event) is False


@pytest.mark.parametrize(
    "platform", ["aiocqhttp", "qq_official", "qq_official_webhook"]
)
def test_event_listener_filter_covers_supported_platforms(platform: str):
    adapter_filter = PlatformAdapterTypeFilter(SUPPORTED_ADAPTER_TYPES)
    event = FakeEvent(platform=platform, bot=OfficialBot())

    assert adapter_filter.filter(event, None) is True


def test_event_listener_filter_skips_other_platforms():
    adapter_filter = PlatformAdapterTypeFilter(SUPPORTED_ADAPTER_TYPES)
    event = FakeEvent(platform="telegram", bot=OfficialBot())

    assert adapter_filter.filter(event, None) is False


def test_plugin_listeners_registered_for_official_platforms():
    """插件里的禁词/刷屏/进退群监听器已放行官方机器人平台"""
    from astrbot.core.star.filter.platform_adapter_type import (
        ADAPTER_NAME_2_TYPE,
        PlatformAdapterType,
        PlatformAdapterTypeFilter,
    )
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    from astrbot_plugin_qqadmin.main import QQAdminPlugin  # noqa: F401

    wanted = {"on_ban_words", "spamming_ban", "event_monitoring"}
    official_flags = (
        PlatformAdapterType.QQOFFICIAL | PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    checked = set()
    for metadata in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, only_activated=False
    ):
        if metadata.handler_name not in wanted:
            continue
        if "astrbot_plugin_qqadmin" not in metadata.handler_module_path:
            continue
        checked.add(metadata.handler_name)
        filters = [
            event_filter
            for event_filter in metadata.event_filters
            if isinstance(event_filter, PlatformAdapterTypeFilter)
        ]
        assert filters, metadata.handler_name
        assert filters[0].platform_type & official_flags == official_flags
        assert filters[0].platform_type & ADAPTER_NAME_2_TYPE["aiocqhttp"]

    assert checked == wanted


def test_mute_member_uses_restrict_chat_setting():
    api, event = official_api()
    before = datetime.now(OFFICIAL_TZ)

    run(api.set_group_ban(user_id=TARGET_ID, duration=600))

    call = only_call(event)
    assert call.method == "POST"
    assert call.path == f"/v2/groups/{GROUP_ID}/restrict_chat_setting"
    assert call.url.endswith(call.path)
    (member,) = call.json["members"]
    assert member["op"] == "add"
    assert member["member_openid"] == TARGET_ID
    expire_at = datetime.fromisoformat(member["mute_expire_at"])
    assert expire_at.utcoffset() == timedelta(hours=8)
    assert timedelta(seconds=601) <= expire_at - before <= timedelta(seconds=603)


@pytest.mark.parametrize(
    ("duration", "low", "high"),
    [
        (1, 2, 4),
        (60, 61, 63),
        (360, 361, 363),
        # 官方上限 30 天，补时不能越过上限
        (OFFICIAL_MAX_BAN_SECONDS, OFFICIAL_MAX_BAN_SECONDS, OFFICIAL_MAX_BAN_SECONDS + 2),
    ],
)
def test_mute_expire_is_not_shorter_than_requested(duration: int, low: int, high: int):
    """官方接口按绝对到期时间计时，秒级截断与请求耗时不能吃掉禁言时长"""
    api, event = official_api()
    before = datetime.now(OFFICIAL_TZ)

    run(api.set_group_ban(user_id=TARGET_ID, duration=duration))

    (member,) = only_call(event).json["members"]
    expire_at = datetime.fromisoformat(member["mute_expire_at"])
    assert expire_at.microsecond == 0
    assert timedelta(seconds=low) <= expire_at - before <= timedelta(seconds=high)


def test_unmute_member_sends_del_op():
    api, event = official_api()

    run(api.set_group_ban(user_id=TARGET_ID, duration=0))

    (member,) = only_call(event).json["members"]
    assert member == {"op": "del", "member_openid": TARGET_ID, "mute_expire_at": ""}


@pytest.mark.parametrize(
    ("reject_add_request", "blacklist"),
    [(False, False), (True, True)],
)
def test_kick_member_uses_batch_remove_members(
    reject_add_request: bool, blacklist: bool
):
    api, event = official_api()

    run(api.set_group_kick(user_id=TARGET_ID, reject_add_request=reject_add_request))

    call = only_call(event)
    assert call.method == "POST"
    assert call.path == f"/v2/groups/{GROUP_ID}/batch_remove_members"
    assert call.json == {
        "member_openids": [TARGET_ID],
        "add_to_member_blacklist": blacklist,
    }


def test_recall_message_uses_delete_message():
    api, event = official_api()

    run(api.delete_msg(message_id="MSG_OPENID_1"))

    call = only_call(event)
    assert call.method == "DELETE"
    assert call.path == f"/v2/groups/{GROUP_ID}/messages/MSG_OPENID_1"
    assert call.json is None


def test_member_info_maps_official_role():
    bot = OfficialBot({"/members/": {"member_role": "admin", "username": "张三"}})
    api = AdminAPI(FakeEvent(bot=bot))

    info = run(api.get_group_member_info(user_id=TARGET_ID))

    assert bot.calls[0].path == f"/v2/groups/{GROUP_ID}/members/{TARGET_ID}"
    assert info["role"] == "admin"
    assert info["nickname"] == "张三"
    assert info["level"] == 0


def test_bot_info_uses_bot_state():
    bot = OfficialBot({"/bot_state": {"member_role": "owner"}})
    api = AdminAPI(FakeEvent(bot=bot))

    info = run(api.get_group_member_info(user_id=BOT_ID))

    assert bot.calls[0].path == f"/v2/groups/{GROUP_ID}/bot_state"
    assert info["role"] == "owner"


UNSUPPORTED_CASES = [
    ("set_group_whole_ban", {"enable": True}, "全员禁言"),
    ("set_group_card", {"user_id": TARGET_ID, "card": "改名"}, "群昵称"),
    ("set_group_special_title", {"user_id": TARGET_ID, "special_title": "头衔"}, "头衔"),
    ("set_group_admin", {"user_id": TARGET_ID, "enable": True}, "管理员"),
    ("set_essence_msg", {"message_id": "1"}, "精华"),
    ("delete_essence_msg", {"message_id": "1"}, "精华"),
    ("get_essence_msg_list", {}, "精华"),
    ("set_group_portrait", {"file": "http://example.com/a.png"}, "群头像"),
    ("set_group_name", {"group_name": "新群名"}, "群名"),
    ("get_group_member_list", {}, "群成员列表"),
    ("get_stranger_info", {"user_id": TARGET_ID}, "用户资料"),
    ("get_group_msg_history", {}, "批量撤回"),
    ("send_group_notice", {"content": "公告"}, "群公告"),
    ("get_group_notice", {}, "群公告"),
    ("get_group_root_files", {}, "群文件"),
    ("get_group_files_by_folder", {"folder_id": "/"}, "群文件"),
    ("create_group_file_folder", {"folder_name": "文件夹"}, "群文件"),
    ("upload_group_file", {"file": "a.txt", "name": "a.txt"}, "群文件"),
    ("delete_group_file", {"file_id": "fid"}, "群文件"),
    ("delete_group_folder", {"folder_id": "fid"}, "群文件"),
]


@pytest.mark.parametrize(("method", "kwargs", "keyword"), UNSUPPORTED_CASES)
def test_unsupported_ops_report_concisely(method: str, kwargs: dict, keyword: str):
    api, event = official_api()

    with pytest.raises(OfficialUnsupportedError) as err:
        run(getattr(api, method)(**kwargs))

    assert keyword in str(err.value)
    assert event.bot.calls == []


def test_onebot_ban_keeps_int_ids():
    api, _event, client = onebot_api()

    run(api.set_group_ban(user_id=USER_NUM, duration=60))

    assert client.called("set_group_ban") == [
        {"group_id": int(GROUP_NUM), "user_id": int(USER_NUM), "duration": 60}
    ]


def test_onebot_kick_keeps_int_ids():
    api, _event, client = onebot_api()

    run(api.set_group_kick(user_id=USER_NUM, reject_add_request=True))

    assert client.called("set_group_kick") == [
        {
            "group_id": int(GROUP_NUM),
            "user_id": int(USER_NUM),
            "reject_add_request": True,
        }
    ]


def test_onebot_title_keeps_default_duration():
    api, _event, client = onebot_api()

    run(api.set_group_special_title(user_id=USER_NUM, special_title="头衔"))

    assert client.called("set_group_special_title") == [
        {
            "group_id": int(GROUP_NUM),
            "user_id": int(USER_NUM),
            "special_title": "头衔",
            "duration": -1,
        }
    ]


def test_onebot_member_info_keeps_no_cache():
    api, _event, client = onebot_api()

    run(api.get_group_member_info(user_id=USER_NUM, no_cache=True))

    assert client.called("get_group_member_info") == [
        {
            "group_id": int(GROUP_NUM),
            "user_id": int(USER_NUM),
            "no_cache": True,
        }
    ]


def test_onebot_notice_and_recall_keep_legacy_interfaces():
    api, _event, client = onebot_api()

    run(api.delete_msg(message_id="10086"))
    run(api.send_group_notice(content="公告", image="/tmp/a.png"))
    run(api.get_group_notice())
    run(api.get_group_root_files())

    assert client.called("delete_msg") == [{"message_id": 10086}]
    assert client.called("_send_group_notice") == [
        {
            "group_id": int(GROUP_NUM),
            "content": "公告",
            "image": "/tmp/a.png",
        }
    ]
    assert client.called("_get_group_notice") == [{"group_id": int(GROUP_NUM)}]
    assert client.called("get_group_root_files") == [{"group_id": int(GROUP_NUM)}]


def test_onebot_supports_ops_unavailable_on_official():
    api, _event, client = onebot_api()

    run(api.set_group_whole_ban(enable=True))
    run(api.get_group_member_list())
    run(api.get_group_msg_history(count=10))

    assert client.called("set_group_whole_ban") == [
        {"group_id": int(GROUP_NUM), "enable": True}
    ]
    assert client.called("get_group_member_list") == [{"group_id": int(GROUP_NUM)}]
    assert client.called("get_group_msg_history") == [{"count": 10}]


def test_metadata_declares_official_platforms():
    """metadata.yaml 声明的支持平台与适配层保持一致"""
    from pathlib import Path

    from astrbot.core.star.star_manager import PluginManager

    plugin_dir = Path(__file__).resolve().parent.parent
    metadata = PluginManager._load_plugin_metadata(str(plugin_dir))

    assert set(metadata.support_platforms) == SUPPORTED_PLATFORMS


def test_official_request_retries_once_on_server_error():
    """官方偶发 5xx（处理失败，请稍后重试）时会重试一次"""
    bot = OfficialBot(
        responses={"/members/": {"member_role": "member", "username": "小明"}},
        errors=[botpy.errors.ServerError("处理失败，请稍后重试")],
    )
    api = AdminAPI(FakeEvent(bot=bot))

    info = run(api.get_group_member_info(user_id=TARGET_ID))

    assert info["role"] == "member"
    assert len(bot.calls) == 2


def test_official_request_reports_repeated_server_error():
    bot = OfficialBot(error=botpy.errors.ServerError("处理失败，请稍后重试"))
    api = AdminAPI(FakeEvent(bot=bot))

    with pytest.raises(OfficialAPIError, match="处理失败"):
        run(api.get_group_member_info(user_id=TARGET_ID))
    assert len(bot.calls) == 2
