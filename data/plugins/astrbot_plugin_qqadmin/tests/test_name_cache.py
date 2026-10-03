"""openid -> 昵称 缓存测试：官方平台展示可读名称"""

import asyncio

import pytest

from astrbot_plugin_qqadmin import utils
from astrbot_plugin_qqadmin.utils import (
    display_user,
    get_nickname,
    recall_name,
    remember_names,
)

from fakes import (
    GROUP_ID,
    FakeDB,
    SENDER_ID,
    TARGET_ID,
    FakeEvent,
    OfficialBot,
    OneBotClient,
)

OTHER_GROUP = "7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clear_cache():
    utils._NAME_CACHE.clear()
    yield
    utils._NAME_CACHE.clear()


def test_remember_and_recall_by_group():
    remember_names(GROUP_ID, {TARGET_ID: "小明", SENDER_ID: ""})

    assert recall_name(GROUP_ID, TARGET_ID) == "小明"
    assert recall_name(GROUP_ID, SENDER_ID) == ""  # 空昵称不入池
    assert recall_name(OTHER_GROUP, TARGET_ID) == ""  # 各群独立
    assert recall_name(GROUP_ID, "UNKNOWN") == ""


def test_get_nickname_falls_back_to_cache():
    remember_names(GROUP_ID, {TARGET_ID: "小明"})
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("应用无接口访问权限")))

    assert run(get_nickname(event, TARGET_ID)) == "小明"
    assert run(get_nickname(event, SENDER_ID)) == SENDER_ID


def test_get_nickname_updates_cache_from_payload():
    event = FakeEvent(
        bot=OfficialBot(),
        raw_data={
            "author": {
                "member_openid": TARGET_ID,
                "member_role": "member",
                "username": "小明",
            },
            "group_openid": GROUP_ID,
        },
    )

    assert run(get_nickname(event, TARGET_ID)) == "小明"
    assert recall_name(GROUP_ID, TARGET_ID) == "小明"


def test_display_user_shows_name_with_openid():
    remember_names(GROUP_ID, {TARGET_ID: "小明"})
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("no permission")))

    assert run(display_user(event, TARGET_ID)) == f"小明({TARGET_ID})"


def test_display_user_falls_back_to_openid():
    event = FakeEvent(bot=OfficialBot(error=RuntimeError("no permission")))

    assert run(display_user(event, TARGET_ID)) == TARGET_ID


def test_display_user_keeps_onebot_format():
    event = FakeEvent(
        platform="aiocqhttp",
        group_id="123456",
        bot=OneBotClient(),
    )
    remember_names("123456", {"654321": "小明"})

    assert run(display_user(event, "654321")) == "654321"


def test_plugin_listener_remembers_names():
    from types import SimpleNamespace

    from astrbot_plugin_qqadmin.main import QQAdminPlugin

    db = FakeDB()
    event = FakeEvent(
        bot=OfficialBot(),
        raw_data={
            "author": {
                "member_openid": SENDER_ID,
                "member_role": "owner",
                "username": "EmptyLava",
            },
            "mentions": [
                {
                    "member_openid": TARGET_ID,
                    "member_role": "member",
                    "username": "小明",
                }
            ],
            "group_openid": GROUP_ID,
        },
    )

    run(QQAdminPlugin.remember_names(SimpleNamespace(db=db), event))

    assert recall_name(GROUP_ID, SENDER_ID) == "EmptyLava"
    assert recall_name(GROUP_ID, TARGET_ID) == "小明"
    assert db.nicknames[GROUP_ID] == {SENDER_ID: "EmptyLava", TARGET_ID: "小明"}

    # 同样的昵称不再重复写库
    run(QQAdminPlugin.remember_names(SimpleNamespace(db=db), event))
    assert len(db.nickname_writes) == 1


def test_plugin_listener_ignores_onebot_events():
    from types import SimpleNamespace

    from astrbot_plugin_qqadmin.main import QQAdminPlugin

    db = FakeDB()
    event = FakeEvent(platform="aiocqhttp", group_id="123456", bot=OneBotClient())

    run(QQAdminPlugin.remember_names(SimpleNamespace(db=db), event))

    assert recall_name("123456", SENDER_ID) == ""
    assert db.nicknames == {}


class _DbConfig:
    """QQAdminDB 需要的最小配置"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.default = {}


def test_nickname_pool_persists_in_sqlite(tmp_path):
    """昵称池落库，重启后从库里预热回内存"""
    from astrbot_plugin_qqadmin.data import QQAdminDB

    db_file = tmp_path / "qqadmin.db"

    async def save():
        db = QQAdminDB(_DbConfig(db_file))
        await db.init()
        await db.save_nicknames(GROUP_ID, {TARGET_ID: "小明", SENDER_ID: ""})
        await db.close()

    async def load() -> dict:
        db = QQAdminDB(_DbConfig(db_file))
        await db.init()
        names = await db.load_nicknames()
        await db.close()
        return names

    run(save())
    assert run(load()) == {GROUP_ID: {TARGET_ID: "小明"}}

    # 模拟插件启动时的预热
    for gid, names in run(load()).items():
        remember_names(gid, names)
    assert recall_name(GROUP_ID, TARGET_ID) == "小明"
