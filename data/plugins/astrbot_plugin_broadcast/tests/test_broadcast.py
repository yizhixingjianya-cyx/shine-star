"""广播插件测试：官方平台适配、群聊缓存（KV）与群聊列表"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_DIR.parent
for path in (PLUGINS_DIR, PLUGIN_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astrbot_plugin_broadcast.main import BroadcastPlugin  # noqa: E402

from fakes import (  # noqa: E402
    GROUP_NUM,
    GROUP_OPENID,
    PLATFORM_ID,
    USER_OPENID,
    FakeContext,
    FakeEvent,
    FakeOfficialClient,
    FakeOneBotClient,
    FakePlatform,
    collect,
)


def run(coro):
    return asyncio.run(coro)


class PluginFactory:
    """构造插件：把插件数据目录指向临时目录"""

    @staticmethod
    def build(tmp_path, platforms: list, config: dict | None = None):
        plugin = BroadcastPlugin.__new__(BroadcastPlugin)
        plugin.context = FakeContext(platforms)
        plugin.config = {"enable_scheduled": False, **(config or {})}
        plugin._sent_today = False
        plugin._cache_file = Path(tmp_path) / "group_cache.json"
        plugin._groups = plugin._load_cache()
        return plugin


def official_platform(client: FakeOfficialClient) -> FakePlatform:
    return FakePlatform("qq_official", PLATFORM_ID, client)


def onebot_platform(client: FakeOneBotClient) -> FakePlatform:
    return FakePlatform("aiocqhttp", "aiocqhttp", client)


# ==================== 群聊缓存（KV） ====================


def test_group_cache_records_group_with_name(tmp_path):
    client = FakeOfficialClient(group_name="测试群")
    plugin = PluginFactory.build(tmp_path, [official_platform(client)])

    run(plugin.remember_group(FakeEvent(PLATFORM_ID, GROUP_OPENID)))

    assert plugin._groups[PLATFORM_ID][GROUP_OPENID]["name"] == "测试群"
    assert client.http_calls == [("GET", f"/v2/groups/{GROUP_OPENID}/info")]
    # 落盘
    saved = json.loads((Path(tmp_path) / "group_cache.json").read_text(encoding="utf-8"))
    assert saved[PLATFORM_ID][GROUP_OPENID]["name"] == "测试群"


def test_group_cache_skips_known_group(tmp_path):
    client = FakeOfficialClient()
    plugin = PluginFactory.build(tmp_path, [official_platform(client)])
    event = FakeEvent(PLATFORM_ID, GROUP_OPENID)

    run(plugin.remember_group(event))
    run(plugin.remember_group(event))

    assert len(client.http_calls) == 1  # 已知群不再查接口


def test_group_cache_loads_from_file(tmp_path):
    cache_file = Path(tmp_path) / "group_cache.json"
    cache_file.write_text(
        json.dumps({PLATFORM_ID: {GROUP_OPENID: {"name": "老群", "seen": "09-27 20:00"}}}),
        encoding="utf-8",
    )
    plugin = PluginFactory.build(tmp_path, [])

    assert plugin._groups[PLATFORM_ID][GROUP_OPENID]["name"] == "老群"


def test_group_cache_from_onebot_group_list(tmp_path):
    client = FakeOneBotClient(
        {"get_group_list": [{"group_id": 123456, "group_name": "OneBot 群"}]}
    )
    plugin = PluginFactory.build(tmp_path, [onebot_platform(client)])

    group_ids = run(plugin._platform_group_ids(onebot_platform(client)))

    assert group_ids == [GROUP_NUM]
    assert plugin._groups["aiocqhttp"][GROUP_NUM]["name"] == "OneBot 群"


# ==================== 群聊列表命令 ====================


def test_list_groups_command(tmp_path):
    client = FakeOfficialClient(group_name="测试群")
    plugin = PluginFactory.build(tmp_path, [official_platform(client)])
    run(plugin.remember_group(FakeEvent(PLATFORM_ID, GROUP_OPENID)))

    (text,) = run(collect(plugin.list_groups(FakeEvent(PLATFORM_ID, GROUP_OPENID))))

    assert text.startswith("【群聊列表】共 1 个群（群名 ｜ 群ID ｜ 记录时间）")
    # 群名、群 ID、平台都带标签，避免"1"这种群名被误认成数量
    assert f"[qq_official:{PLATFORM_ID}] 测试群 ｜ {GROUP_OPENID} ｜" in text


def test_list_groups_command_refreshes_group_name(tmp_path):
    """群改名后，列表会现场刷新成新群名"""
    client = FakeOfficialClient(group_name="新群名")
    platform = official_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    plugin._groups = {
        PLATFORM_ID: {GROUP_OPENID: {"name": "旧群名", "seen": "09-28 01:00"}}
    }

    (text,) = run(collect(plugin.list_groups(FakeEvent(PLATFORM_ID, GROUP_OPENID))))

    assert "新群名" in text
    assert "旧群名" not in text
    assert plugin._groups[PLATFORM_ID][GROUP_OPENID]["name"] == "新群名"
    saved = json.loads((Path(tmp_path) / "group_cache.json").read_text(encoding="utf-8"))
    assert saved[PLATFORM_ID][GROUP_OPENID]["name"] == "新群名"


def test_list_groups_command_without_loaded_platform(tmp_path):
    """平台未加载时用缓存里的群名兜底"""
    plugin = PluginFactory.build(tmp_path, [])
    plugin._groups = {
        PLATFORM_ID: {GROUP_OPENID: {"name": "缓存群名", "seen": "09-28 01:00"}}
    }

    (text,) = run(collect(plugin.list_groups(FakeEvent(PLATFORM_ID, GROUP_OPENID))))

    assert f"[{PLATFORM_ID}] 缓存群名 ｜ {GROUP_OPENID}" in text


def test_list_groups_command_when_empty(tmp_path):
    plugin = PluginFactory.build(tmp_path, [])

    (text,) = run(collect(plugin.list_groups(FakeEvent(PLATFORM_ID, GROUP_OPENID))))

    assert "暂无记录" in text


# ==================== 发送路径 ====================


def test_official_group_send_uses_bot_api(tmp_path):
    client = FakeOfficialClient()
    platform = official_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])

    run(plugin._send_group_text(platform, GROUP_OPENID, "公告内容"))

    (post,) = client.posts
    assert post["group_openid"] == GROUP_OPENID
    assert post["content"] == "公告内容"
    assert post["msg_type"] == 0
    assert plugin.context.sent == []


def test_onebot_group_send_uses_valid_umo(tmp_path):
    """umo 必须是 AstrBot 认识的格式（曾经拼成 :group: 导致发送失败）"""
    from astrbot.core.platform.message_session import MessageSesion

    client = FakeOneBotClient()
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])

    run(plugin._send_group_text(platform, GROUP_NUM, "公告内容"))

    (umo, text) = plugin.context.sent[0]
    assert text == "公告内容"
    session = MessageSesion.from_str(umo)  # 不能抛异常
    assert session.platform_name == "aiocqhttp"
    assert session.session_id == GROUP_NUM
    assert str(session.message_type) == "MessageType.GROUP_MESSAGE"


# ==================== 定时群发 / 启动通知 ====================


def test_scheduled_send_to_official_whitelist(tmp_path):
    client = FakeOfficialClient()
    platform = official_platform(client)
    plugin = PluginFactory.build(
        tmp_path,
        [platform],
        {"scheduled_content": "早安", "scheduled_group_ids": [GROUP_OPENID]},
    )
    run(plugin.remember_group(FakeEvent(PLATFORM_ID, GROUP_OPENID)))

    run(plugin._send_scheduled())

    (post,) = client.posts
    assert post["group_openid"] == GROUP_OPENID
    assert post["content"] == "早安"


def test_scheduled_send_to_onebot_whitelist(tmp_path):
    client = FakeOneBotClient()
    platform = onebot_platform(client)
    plugin = PluginFactory.build(
        tmp_path,
        [platform],
        {"scheduled_content": "早安", "scheduled_group_ids": [GROUP_NUM]},
    )

    run(plugin._send_scheduled())

    (umo, text) = plugin.context.sent[0]
    assert text == "早安"
    assert umo == f"aiocqhttp:GroupMessage:{GROUP_NUM}"


def test_startup_notify_uses_cache_for_official(tmp_path):
    client = FakeOfficialClient(group_name="测试群")
    platform = official_platform(client)
    plugin = PluginFactory.build(
        tmp_path, [platform], {"startup_content": "已启动"}
    )
    run(plugin.remember_group(FakeEvent(PLATFORM_ID, GROUP_OPENID)))

    run(plugin._startup_notify())

    assert [post["group_openid"] for post in client.posts] == [GROUP_OPENID]
    assert client.called("get_group_list") if hasattr(client, "called") else True


def test_startup_notify_uses_group_list_for_onebot(tmp_path):
    client = FakeOneBotClient(
        {"get_group_list": [{"group_id": int(GROUP_NUM), "group_name": "OneBot 群"}]}
    )
    plugin = PluginFactory.build(
        tmp_path, [onebot_platform(client)], {"startup_content": "已启动"}
    )

    run(plugin._startup_notify())

    (umo, text) = plugin.context.sent[0]
    assert umo == f"aiocqhttp:GroupMessage:{GROUP_NUM}"
    assert text == "已启动"


# ==================== /广播 指令 ====================


def test_broadcast_to_official_whitelisted_group(tmp_path):
    client = FakeOfficialClient()
    platform = official_platform(client)
    plugin = PluginFactory.build(
        tmp_path, [platform], {"scheduled_group_ids": [GROUP_OPENID]}
    )
    event = FakeEvent(PLATFORM_ID, GROUP_OPENID, f"/广播 {GROUP_OPENID} 你好")

    (reply,) = run(collect(plugin.broadcast(event)))

    assert "已发送到群" in reply
    assert client.posts[0]["content"] == "你好"


def test_broadcast_to_official_group_without_whitelist(tmp_path):
    """白名单只限制定时群发，/广播 不受它限制"""
    client = FakeOfficialClient()
    platform = official_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform], {"scheduled_group_ids": []})
    event = FakeEvent(PLATFORM_ID, GROUP_OPENID, f"/广播 {GROUP_OPENID} 你好")

    (reply,) = run(collect(plugin.broadcast(event)))

    assert "已发送到群" in reply
    assert client.posts[0]["content"] == "你好"


def test_broadcast_to_onebot_group_without_whitelist(tmp_path):
    client = FakeOneBotClient({"get_group_info": {"group_name": "OneBot 群"}})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    event = FakeEvent("aiocqhttp", GROUP_NUM, f"/广播 {GROUP_NUM} 你好")

    (reply,) = run(collect(plugin.broadcast(event)))

    assert "已发送到群" in reply
    assert plugin.context.sent[0] == (f"aiocqhttp:GroupMessage:{GROUP_NUM}", "你好")


def test_broadcast_to_onebot_group(tmp_path):
    client = FakeOneBotClient({"get_group_info": {"group_name": "OneBot 群"}})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(
        tmp_path, [platform], {"scheduled_group_ids": [GROUP_NUM]}
    )
    event = FakeEvent("aiocqhttp", GROUP_NUM, f"/广播 {GROUP_NUM} 你好")

    (reply,) = run(collect(plugin.broadcast(event)))

    assert "已发送到群" in reply
    assert plugin.context.sent[0] == (f"aiocqhttp:GroupMessage:{GROUP_NUM}", "你好")


def test_broadcast_to_onebot_private_user(tmp_path):
    client = FakeOneBotClient(fail_actions={"get_group_info"})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    event = FakeEvent("aiocqhttp", GROUP_NUM, f"/广播 {GROUP_NUM} 私聊内容")

    (reply,) = run(collect(plugin.broadcast(event, target_id="9999", content="私聊内容")))

    assert "已发送到个人" in reply
    assert plugin.context.sent[0] == ("aiocqhttp:FriendMessage:9999", "私聊内容")


def test_scheduled_send_falls_back_to_ready_platform(tmp_path):
    """白名单里的群不在缓存时，按已就绪的平台尝试发送"""
    client = FakeOfficialClient()
    plugin = PluginFactory.build(
        tmp_path,
        [official_platform(client)],
        {"scheduled_content": "早安", "scheduled_group_ids": [GROUP_OPENID]},
    )

    run(plugin._send_scheduled())

    assert client.posts[0]["group_openid"] == GROUP_OPENID


def test_scheduled_send_skips_when_content_or_whitelist_empty(tmp_path):
    client = FakeOfficialClient()
    plugin = PluginFactory.build(
        tmp_path, [official_platform(client)], {"scheduled_group_ids": [GROUP_OPENID]}
    )

    run(plugin._send_scheduled())  # 内容为空

    assert client.posts == []


def test_scheduled_send_requires_whitelist(tmp_path):
    """定时群发仍然只发给白名单内的群"""
    client = FakeOfficialClient()
    plugin = PluginFactory.build(
        tmp_path,
        [official_platform(client)],
        {"scheduled_content": "早安", "scheduled_group_ids": []},
    )

    run(plugin._send_scheduled())

    assert client.posts == []


def test_broadcast_command_requires_admin(tmp_path):
    """广播指令挂上 AstrBot 管理员权限过滤器"""
    from astrbot.core.star.filter.permission import (
        PermissionType,
        PermissionTypeFilter,
    )
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    for metadata in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, only_activated=False
    ):
        if metadata.handler_name != "broadcast":
            continue
        if "astrbot_plugin_broadcast" not in metadata.handler_module_path:
            continue
        filters = [
            f for f in metadata.event_filters if isinstance(f, PermissionTypeFilter)
        ]
        assert filters, "广播指令缺少权限过滤器"
        assert filters[0].permission_type == PermissionType.ADMIN
        return
    pytest.fail("未找到广播指令的处理器注册信息")


def test_broadcast_by_group_name(tmp_path):
    """/广播 群名 内容：按缓存里的群名匹配目标"""
    client = FakeOneBotClient({"get_group_info": {"group_name": "OneBot测试群"}})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    run(plugin.remember_group(FakeEvent("aiocqhttp", GROUP_NUM)))

    (reply,) = run(collect(plugin.broadcast(FakeEvent("aiocqhttp", GROUP_NUM, "/广播 OneBot测试群 test"))))

    assert "已发送到群" in reply
    assert plugin.context.sent[0] == (f"aiocqhttp:GroupMessage:{GROUP_NUM}", "test")


def test_broadcast_by_group_name_with_spaces(tmp_path):
    """群名带空格时按原始消息切分"""
    client = FakeOfficialClient(group_name="测试 群")
    platform = official_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    run(plugin.remember_group(FakeEvent(PLATFORM_ID, GROUP_OPENID)))

    (reply,) = run(collect(plugin.broadcast(FakeEvent(PLATFORM_ID, GROUP_OPENID, "/广播 测试 群 hello world"))))

    assert "已发送到群" in reply
    assert client.posts[0]["content"] == "hello world"


def test_broadcast_by_group_name_ignores_case(tmp_path):
    client = FakeOneBotClient({"get_group_info": {"group_name": "TestGroup"}})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    run(plugin.remember_group(FakeEvent("aiocqhttp", GROUP_NUM)))

    (reply,) = run(collect(plugin.broadcast(FakeEvent("aiocqhttp", GROUP_NUM, "/广播 testgroup 内容"))))

    assert "已发送到群" in reply
    assert plugin.context.sent[0][1] == "内容"


def test_broadcast_ambiguous_group_name(tmp_path):
    """同名多群时提示改用群 ID"""
    client = FakeOneBotClient()
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])
    plugin._groups = {
        "aiocqhttp": {"111": {"name": "重名群", "seen": "x"}, "222": {"name": "重名群", "seen": "x"}}
    }

    (reply,) = run(collect(plugin.broadcast(FakeEvent("aiocqhttp", "111", "/广播 重名群 内容"))))

    assert "匹配到 2 个名为「重名群」的群" in reply
    assert "111" in reply and "222" in reply
    assert plugin.context.sent == []


def test_broadcast_unknown_name_reports_clearly(tmp_path):
    """非数字且不匹配任何群名时，明确报错而不是当成私聊"""
    client = FakeOneBotClient(fail_actions={"get_group_info"})
    platform = onebot_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])

    (reply,) = run(collect(plugin.broadcast(FakeEvent("aiocqhttp", GROUP_NUM, "/广播 不存在的群 内容"))))

    assert "未找到群名为「不存在的群」的群" in reply
    assert plugin.context.sent == []


def test_broadcast_unknown_name_on_official_reports_clearly(tmp_path):
    """官方平台上打错的群名不应被当成 openid 丢给接口"""
    client = FakeOfficialClient()
    platform = official_platform(client)
    plugin = PluginFactory.build(tmp_path, [platform])

    (reply,) = run(collect(plugin.broadcast(FakeEvent(PLATFORM_ID, GROUP_OPENID, "/广播 测试群 内容"))))

    assert "未找到群名为「测试群」的群" in reply
    assert client.posts == []
    assert client.http_calls == []
