"""权限测试：官方机器人角色映射、命令放行与不支持提示"""

import asyncio

import pytest

from astrbot_plugin_qqadmin.admin_api import AdminAPI
from astrbot_plugin_qqadmin.permission import PermLevel, perm_manager, perm_required

from fakes import BOT_ID, SENDER_ID, FakeConfig, FakeDB, FakeEvent, OfficialBot, OneBotClient


def run(coro):
    return asyncio.run(coro)


def collect(generator) -> list[str]:
    async def _collect() -> list[str]:
        return [result.chain[0].text async for result in generator]

    return asyncio.run(_collect())


@pytest.fixture
def permissions():
    perm_manager.refresh(FakeConfig(), FakeDB(group_config={"level_threshold": 50}))
    yield perm_manager


def official_event(role: str = "owner", bot_role: str = "owner") -> FakeEvent:
    return FakeEvent(
        bot=OfficialBot(
            {
                "/bot_state": {"member_role": bot_role},
                "/members/": {"member_role": role, "username": "群友"},
            }
        )
    )


def onebot_event(role: str = "owner") -> FakeEvent:
    return FakeEvent(
        platform="aiocqhttp",
        group_id="123456",
        sender_id="654321",
        self_id="111111",
        bot=OneBotClient(
            {"get_group_member_info": {"role": role, "level": 99, "nickname": "群友"}}
        ),
    )


class DummyPlugin:
    """用于验证权限装饰器的假插件"""

    @perm_required(PermLevel.MEMBER)
    async def ok(self, event):
        yield event.plain_result("已执行")

    @perm_required(PermLevel.MEMBER)
    async def whole_ban(self, event):
        await AdminAPI(event).set_group_whole_ban(enable=True)
        yield event.plain_result("已开启全体禁言")


@pytest.mark.parametrize(
    ("role", "level"),
    [
        ("owner", PermLevel.OWNER),
        ("admin", PermLevel.ADMIN),
        ("member", PermLevel.MEMBER),
    ],
)
def test_official_member_role_maps_to_perm_level(permissions, role: str, level):
    event = official_event(role=role)

    assert run(permissions.get_perm_level(event, SENDER_ID)) == level


def test_official_bot_role_comes_from_bot_state(permissions):
    event = official_event(role="member", bot_role="admin")

    assert run(permissions.get_perm_level(event, BOT_ID)) == PermLevel.ADMIN


def test_official_unknown_role_when_member_api_unavailable(permissions):
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("11253 该能力正在内邀接入中")))

    assert run(permissions.get_perm_level(event, SENDER_ID)) == PermLevel.UNKNOWN


def test_official_command_runs_for_group_owner(permissions):
    plugin = DummyPlugin()
    event = official_event()

    assert collect(plugin.ok(event)) == ["已执行"]
    assert event.is_stopped() is False


def test_official_command_denied_when_role_unknown(permissions):
    plugin = DummyPlugin()
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("11253 该能力正在内邀接入中")))

    assert collect(plugin.ok(event)) == ["你没管理员权限"]
    assert event.is_stopped() is True


def test_official_command_denied_for_ordinary_member(permissions):
    plugin = DummyPlugin()
    event = official_event(role="member", bot_role="owner")

    assert collect(plugin.ok(event)) == ["你没管理员权限"]


def test_official_command_reports_unsupported_operation(permissions):
    plugin = DummyPlugin()
    event = official_event()

    assert collect(plugin.whole_ban(event)) == ["官方机器人暂不支持全员禁言"]
    # 只有权限查询走了官方接口，没有发起禁言请求
    assert {call.method for call in event.bot.calls} == {"GET"}
    assert event.is_stopped() is True


def test_other_platform_command_is_ignored(permissions):
    plugin = DummyPlugin()
    event = FakeEvent(platform="telegram", bot=OfficialBot())

    assert collect(plugin.ok(event)) == []


def test_onebot_command_runs_for_group_owner(permissions):
    plugin = DummyPlugin()
    event = onebot_event(role="owner")

    assert collect(plugin.ok(event)) == ["已执行"]


def test_onebot_command_denied_for_ordinary_member(permissions):
    plugin = DummyPlugin()
    event = onebot_event(role="member")

    assert collect(plugin.ok(event)) == ["你没管理员权限"]
    assert event.is_stopped() is True
