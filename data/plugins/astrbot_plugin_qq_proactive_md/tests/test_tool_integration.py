"""工具层联调测试：在模拟的 AstrBot 运行时中真实执行插件注册的三个工具。

覆盖：工具注册与 Schema、场景自动识别、目标会话解析、权限校验、
QQ 原生 Markdown / 按钮 / Ark 的实际请求载荷、Markdown 降级与被动转主动。

运行方式（标准库 unittest，无需 pytest）：

    python tests/test_tool_integration.py

说明：本机没有 QQ 机器人的 appid/secret，也没有可用的 QQ 网关，因此无法对开放
平台做真实网络调用；这里用假的 ``platform.get_client().api`` 承接所有发送请求，
断言到「最终 HTTP 请求体」这一层，等价于联调中的协议校验。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import astrbot_stub  # noqa: E402

astrbot_stub.bootstrap()

import jsonschema  # noqa: E402
from astrbot.core.agent.run_context import ContextWrapper  # noqa: E402
from fakes import (  # noqa: E402
    FakeAgentContext,
    FakeEvent,
    FakePlatform,
    FakeStarContext,
    RawC2CMessage,
    RawChannelMessage,
    RawDirectMessage,
    RawGroupMessage,
)

main = astrbot_stub.load_main_module()
# 与 main.py 使用同一个 qq_md_core 实例，避免出现两份模块对象。
core = astrbot_stub.load_core_module()
MARKDOWN_NOT_ALLOWED_ERROR = core.MARKDOWN_NOT_ALLOWED_ERROR
SCENE_C2C = core.SCENE_C2C
SCENE_GROUP = core.SCENE_GROUP

try:
    import botpy.errors as botpy_errors
except Exception:  # pragma: no cover - 未安装 qq-botpy 时退化为普通异常
    botpy_errors = None

TOOL_SLUGS = ("qq_send_markdown", "qq_send_ark", "qq_get_session_info")


def stub_star_context():
    """返回桩 Star 上下文（仅实现 add_llm_tools）。"""
    import astrbot.api.star as star_stub

    return star_stub.Context()


def make_runtime(
    *,
    platform_name: str = "qq_official",
    unified_msg_origin: str = "qq_main:GroupMessage:B1A2C3D4E5",
    raw_message=None,
    message_id: str | None = "USER_MSG_ID",
    role: str = "member",
    register_platforms: bool = True,
):
    """构造一套（工具上下文, 假平台）组合。

    Args:
        platform_name: 平台适配器名称。
        unified_msg_origin: 当前会话。
        raw_message: QQ 原始消息对象，用于场景识别。
        message_id: 当前消息 ID。
        role: 当前用户角色。
        register_platforms: 是否把平台注册到 Star 上下文中。

    Returns:
        ``(tool_context, platform)`` 二元组。
    """
    platform = FakePlatform(name=platform_name)
    star_context = FakeStarContext({"qq_main": platform} if register_platforms else {})
    event = FakeEvent(
        unified_msg_origin=unified_msg_origin,
        raw_message=raw_message,
        message_id=message_id,
        role=role,
    )
    wrapper = ContextWrapper(context=FakeAgentContext(star_context, event))
    return wrapper, platform


def markdown_rejected_error():
    """构造平台拒绝原生 Markdown 时抛出的异常。"""
    if botpy_errors:
        return botpy_errors.ServerError(MARKDOWN_NOT_ALLOWED_ERROR)
    return RuntimeError(MARKDOWN_NOT_ALLOWED_ERROR)


class ToolRegistryTests(unittest.TestCase):
    def test_build_tools_names_and_schema(self):
        tools = main.build_tools({})
        self.assertEqual([tool.name for tool in tools], list(TOOL_SLUGS))
        for tool in tools:
            self.assertTrue(tool.description)
            jsonschema.Draft202012Validator.check_schema(tool.parameters)
            self.assertEqual(tool.parameters.get("type"), "object")
        self.assertTrue(tools[0].markdown_fallback_to_plain)

    def test_build_tools_honours_config(self):
        tools = main.build_tools({"markdown_fallback_to_plain": False})
        self.assertFalse(tools[0].markdown_fallback_to_plain)
        self.assertFalse(tools[1].markdown_fallback_to_plain)

    def test_plugin_registers_tools(self):
        context = stub_star_context()
        plugin = main.QQProactiveMdPlugin(context, {"markdown_fallback_to_plain": False})
        self.assertEqual([tool.name for tool in context.tools], list(TOOL_SLUGS))
        self.assertFalse(context.tools[0].markdown_fallback_to_plain)
        self.assertIs(plugin.context, context)

    def test_markdown_schema_declares_key_parameters(self):
        properties = main.build_tools({})[0].parameters["properties"]
        for key in ("content", "template_id", "template_params", "keyboard_id", "buttons", "session", "scene", "proactive"):
            self.assertIn(key, properties)
        self.assertEqual(properties["buttons"]["items"]["required"], ["label"])
        self.assertIn(SCENE_GROUP, properties["scene"]["enum"])
        self.assertIn(SCENE_C2C, properties["scene"]["enum"])

    def test_schemas_are_json_serialisable(self):
        for tool in main.build_tools({}):
            payload = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            self.assertIsInstance(json.dumps(payload), str)


class SendMarkdownIntegrationTests(unittest.TestCase):
    def test_group_passive_reply_payload(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        result = asyncio.run(
            tool.call(context, content="# 早安\n**重点**", buttons=[{"label": "打卡"}])
        )
        method, payload = platform.api.last_call()
        self.assertEqual(method, "post_group_message")
        self.assertEqual(payload["group_openid"], "B1A2C3D4E5")
        self.assertEqual(payload["msg_type"], 2)
        self.assertEqual(payload["markdown"], {"content": "# 早安\n**重点**"})
        self.assertEqual(payload["msg_id"], "USER_MSG_ID")
        self.assertIn("msg_seq", payload)
        self.assertEqual(
            payload["keyboard"]["content"]["rows"][0]["buttons"][0]["render_data"]["label"],
            "打卡",
        )
        self.assertIn("已以被动回复方式发送", result)

    def test_proactive_push_omits_msg_id(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        asyncio.run(tool.call(context, content="主动打招呼", proactive=True))
        _, payload = platform.api.last_call()
        self.assertNotIn("msg_id", payload)
        self.assertIn("msg_seq", payload)

    def test_proactive_push_when_no_current_message(self):
        # 定时任务等场景下事件没有可回复的消息 ID，应自动转为主动推送。
        context, platform = make_runtime(message_id=None)
        tool = main.build_tools({})[0]
        result = asyncio.run(tool.call(context, content="定时播报"))
        _, payload = platform.api.last_call()
        self.assertNotIn("msg_id", payload)
        self.assertIn("主动推送", result)

    def test_scene_auto_detection(self):
        cases = (
            (RawGroupMessage(), "qq_main:GroupMessage:B1A2C3D4E5", "post_group_message"),
            (RawC2CMessage(), "qq_main:FriendMessage:A1B2C3D4E5", "post_c2c_message"),
            (RawChannelMessage(), "qq_main:GroupMessage:123456", "post_message"),
            (RawDirectMessage(), "qq_main:FriendMessage:987654", "post_dms"),
        )
        for raw, umo, expected_method in cases:
            with self.subTest(umo=umo):
                context, platform = make_runtime(unified_msg_origin=umo, raw_message=raw)
                tool = main.build_tools({})[0]
                asyncio.run(tool.call(context, content="hi"))
                method, _ = platform.api.last_call()
                self.assertEqual(method, expected_method)

    def test_explicit_scene_overrides_detection(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        asyncio.run(tool.call(context, content="hi", scene=SCENE_C2C, proactive=True))
        method, payload = platform.api.last_call()
        self.assertEqual(method, "post_c2c_message")
        self.assertEqual(payload["openid"], "B1A2C3D4E5")

    def test_target_session_uses_explicit_id(self):
        context, platform = make_runtime(
            unified_msg_origin="qq_main:FriendMessage:A1B2C3D4E5",
            raw_message=RawC2CMessage(),
            role="admin",
        )
        tool = main.build_tools({})[0]
        asyncio.run(
            tool.call(
                context,
                content="hi",
                session="qq_main:GroupMessage:DEADBEEF",
                proactive=True,
                scene=SCENE_GROUP,
            )
        )
        method, payload = platform.api.last_call()
        self.assertEqual(method, "post_group_message")
        self.assertEqual(payload["group_openid"], "DEADBEEF")

    def test_cross_session_requires_admin(self):
        context, platform = make_runtime(role="member")
        tool = main.build_tools({})[0]
        result = asyncio.run(
            tool.call(context, content="hi", session="qq_main:GroupMessage:OTHER")
        )
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])

    def test_keyboard_id_and_buttons_conflict(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        result = asyncio.run(
            tool.call(context, content="hi", keyboard_id="1", buttons=[{"label": "a"}])
        )
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])

    def test_missing_content_and_template(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        result = asyncio.run(tool.call(context))
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])

    def test_template_markdown_payload(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[0]
        asyncio.run(
            tool.call(
                context,
                template_id="tpl_9",
                template_params={"title": "标题"},
                proactive=True,
            )
        )
        _, payload = platform.api.last_call()
        self.assertEqual(payload["markdown"]["custom_template_id"], "tpl_9")
        self.assertEqual(payload["markdown"]["params"], [{"key": "title", "values": ["标题"]}])

    def test_invalid_session_string(self):
        context, platform = make_runtime(role="admin")
        tool = main.build_tools({})[0]
        result = asyncio.run(tool.call(context, content="hi", session="not-a-session"))
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])

    def test_non_qq_platform_rejected(self):
        context, platform = make_runtime(platform_name="telegram")
        tool = main.build_tools({})[0]
        result = asyncio.run(tool.call(context, content="hi"))
        self.assertTrue(result.startswith("error:"))
        self.assertIn("QQ Official Bot", result)
        self.assertEqual(platform.api.calls, [])

    def test_missing_platform_instance(self):
        context, platform = make_runtime(register_platforms=False)
        tool = main.build_tools({})[0]
        result = asyncio.run(tool.call(context, content="hi"))
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])

    def test_markdown_rejected_falls_back_to_plain(self):
        context, platform = make_runtime()
        platform.api.fail_next("post_group_message", markdown_rejected_error())
        tool = main.build_tools({})[0]
        asyncio.run(tool.call(context, content="# 标题\n**重点**", proactive=True))
        self.assertEqual(len(platform.api.calls), 2)
        _, fallback = platform.api.calls[1]
        self.assertEqual(fallback["msg_type"], 0)
        self.assertEqual(fallback["content"], "标题\n重点")
        self.assertNotIn("markdown", fallback)

    def test_markdown_fallback_disabled(self):
        context, platform = make_runtime()
        platform.api.fail_next("post_group_message", markdown_rejected_error())
        tool = main.build_tools({"markdown_fallback_to_plain": False})[0]
        result = asyncio.run(tool.call(context, content="# 标题", proactive=True))
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(len(platform.api.calls), 1)

    def test_stale_msg_id_retries_as_proactive(self):
        context, platform = make_runtime()
        exc = (
            botpy_errors.ForbiddenError("forbidden")
            if botpy_errors
            else RuntimeError("forbidden msg_id")
        )
        platform.api.fail_next("post_group_message", exc)
        tool = main.build_tools({})[0]
        asyncio.run(tool.call(context, content="hi"))
        self.assertEqual(len(platform.api.calls), 2)
        _, retry = platform.api.calls[1]
        self.assertNotIn("msg_id", retry)
        self.assertEqual(retry["msg_type"], 2)


class SendArkIntegrationTests(unittest.TestCase):
    def test_send_ark_payload(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[1]
        result = asyncio.run(
            tool.call(
                context,
                template_id=37,
                kv=[{"key": "title", "value": "标题"}],
                proactive=True,
            )
        )
        method, payload = platform.api.last_call()
        self.assertEqual(method, "post_group_message")
        self.assertEqual(payload["msg_type"], 3)
        self.assertEqual(
            payload["ark"],
            {"template_id": 37, "kv": [{"key": "title", "value": "标题"}]},
        )
        self.assertNotIn("markdown", payload)
        self.assertIn("Ark", result)

    def test_missing_template_id(self):
        context, platform = make_runtime()
        tool = main.build_tools({})[1]
        result = asyncio.run(tool.call(context))
        self.assertTrue(result.startswith("error:"))
        self.assertEqual(platform.api.calls, [])


class SessionInfoIntegrationTests(unittest.TestCase):
    def test_group_session_info(self):
        context, _ = make_runtime()
        tool = main.build_tools({})[2]
        result = asyncio.run(tool.call(context))
        self.assertIn("scene: QQ 群聊", result)
        self.assertIn("target_id: B1A2C3D4E5", result)
        self.assertIn("proactive_push_supported: yes", result)
        self.assertIn("current_message_id: USER_MSG_ID", result)

    def test_c2c_session_info(self):
        context, _ = make_runtime(
            unified_msg_origin="qq_main:FriendMessage:A1B2C3D4E5",
            raw_message=RawC2CMessage(),
        )
        tool = main.build_tools({})[2]
        result = asyncio.run(tool.call(context))
        self.assertIn("scene: QQ 单聊", result)
        self.assertIn("message_type: FriendMessage", result)

    def test_session_info_on_other_platform(self):
        context, _ = make_runtime(platform_name="discord")
        tool = main.build_tools({})[2]
        result = asyncio.run(tool.call(context))
        self.assertIn("discord", result)
        self.assertIn("unavailable", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
