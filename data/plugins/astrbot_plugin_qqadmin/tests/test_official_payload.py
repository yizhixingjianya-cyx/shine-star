"""官方群消息负载测试：成员角色与 @ 目标解析

负载结构取自官方文档 GROUP_AT_MESSAGE_CREATE 事件示例：
author / mentions 都是带 member_openid、member_role 的 User 对象。
"""

import asyncio

import pytest
from astrbot.api.message_components import At

from astrbot_plugin_qqadmin.admin_api import (
    OFFICIAL_PLATFORMS,
    AdminAPI,
    OfficialAPIError,
    official_users,
)
from astrbot_plugin_qqadmin.permission import PermLevel, perm_manager, perm_required
from astrbot_plugin_qqadmin.utils import get_ats

from fakes import (
    BOT_ID,
    GROUP_ID,
    SENDER_ID,
    TARGET_ID,
    FakeConfig,
    FakeDB,
    FakeEvent,
    OfficialBot,
)


def run(coro):
    return asyncio.run(coro)


def collect(generator) -> list[str]:
    async def _collect() -> list[str]:
        return [result.chain[0].text async for result in generator]

    return asyncio.run(_collect())


def payload(
    sender_role: str = "owner",
    target_role: str = "member",
    bot_role: str = "admin",
) -> dict:
    return {
        "id": "ROBOT1.0_MSGID",
        "author": {
            "id": SENDER_ID,
            "member_openid": SENDER_ID,
            "member_role": sender_role,
            "username": "EmptyLava",
            "bot": False,
        },
        "content": f"/投票禁言 60 <@{TARGET_ID}>",
        "group_openid": GROUP_ID,
        "message_type": 0,
        "mentions": [
            {
                "id": BOT_ID,
                "member_openid": BOT_ID,
                "member_role": bot_role,
                "username": "机器人",
                "bot": True,
                "is_you": True,
            },
            {
                "id": TARGET_ID,
                "member_openid": TARGET_ID,
                "member_role": target_role,
                "username": "小明",
                "bot": False,
            },
        ],
    }


def official_event(
    sender_role: str = "owner",
    target_role: str = "member",
    bot_role: str = "admin",
    platform: str = "qq_official",
) -> FakeEvent:
    return FakeEvent(
        platform=platform,
        bot=OfficialBot(),
        message_str=f"/投票禁言 60 <@{TARGET_ID}>",
        raw_data=payload(sender_role, target_role, bot_role),
    )


@pytest.fixture
def permissions():
    perm_manager.refresh(FakeConfig(), FakeDB(group_config={"level_threshold": 50}))
    yield perm_manager


class DummyPlugin:
    """用于验证权限装饰器的假插件"""

    @perm_required(PermLevel.MEMBER)
    async def ok(self, event):
        yield event.plain_result("已执行")

    @perm_required(PermLevel.ADMIN)
    async def admin_only(self, event):
        yield event.plain_result("已执行")


def test_official_users_reads_roles_and_mentions():
    users = official_users(official_event())

    assert users[SENDER_ID]["role"] == "owner"
    assert users[SENDER_ID]["nickname"] == "EmptyLava"
    assert users[SENDER_ID]["mention"] is False
    assert users[TARGET_ID]["role"] == "member"
    assert users[TARGET_ID]["nickname"] == "小明"
    assert users[TARGET_ID]["mention"] is True
    assert users[BOT_ID]["mention"] is True


def test_official_users_without_payload():
    assert official_users(FakeEvent()) == {}
    assert official_users(FakeEvent(platform="aiocqhttp")) == {}


@pytest.mark.parametrize("platform", sorted(OFFICIAL_PLATFORMS))
def test_member_info_uses_payload_role(platform: str):
    event = official_event(platform=platform)

    info = run(AdminAPI(event).get_group_member_info(user_id=SENDER_ID))

    assert info["role"] == "owner"
    assert info["nickname"] == "EmptyLava"
    assert event.bot.calls == []


def test_member_info_falls_back_to_bot_state():
    event = FakeEvent(bot=OfficialBot({"/bot_state": {"member_role": "admin"}}))

    info = run(AdminAPI(event).get_group_member_info(user_id=BOT_ID))

    assert info["role"] == "admin"
    assert event.bot.calls[0].path == f"/v2/groups/{GROUP_ID}/bot_state"


def test_member_info_reports_concise_api_error():
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("应用无接口访问权限")))

    with pytest.raises(OfficialAPIError, match="官方机器人接口调用失败"):
        run(AdminAPI(event).get_group_member_info(user_id=TARGET_ID))


def test_get_ats_reads_official_mentions():
    assert get_ats(official_event()) == [TARGET_ID]


def test_get_ats_reads_official_mention_text():
    event = FakeEvent(message_str=f"/禁言 60 <@!{TARGET_ID}>")

    assert get_ats(event) == [TARGET_ID]


def test_get_ats_ignores_author_and_self():
    event = official_event()
    event.message_str = "/禁言 60"

    assert get_ats(event) == [TARGET_ID]
    assert get_ats(FakeEvent(raw_data={"author": payload()["author"]})) == []


def test_get_ats_keeps_onebot_behaviour():
    event = FakeEvent(platform="aiocqhttp", message=[At(qq=TARGET_ID)])

    assert get_ats(event) == [TARGET_ID]
    assert get_ats(FakeEvent(platform="aiocqhttp", message_str="<@123456>")) == []


def test_official_owner_command_runs_by_payload_role(permissions):
    plugin = DummyPlugin()

    assert collect(plugin.ok(official_event(sender_role="owner"))) == ["已执行"]


def test_official_member_command_denied_by_payload_role(permissions):
    plugin = DummyPlugin()
    event = official_event(sender_role="member")

    assert collect(plugin.ok(event)) == ["你没管理员权限"]
    assert event.is_stopped() is True


def test_official_bot_role_denied_by_bot_state(permissions):
    plugin = DummyPlugin()
    event = official_event(sender_role="owner", bot_role="member")

    assert collect(plugin.admin_only(event)) == ["我没管理员权限"]
