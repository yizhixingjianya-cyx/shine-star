"""``qq_md_core`` 的核心逻辑测试：载荷构造、场景分发、降级与重试。

这一层不依赖 AstrBot，可直接用标准库 unittest 运行：

    python tests/test_qq_md_core.py
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from qq_md_core import (  # noqa: E402
    MARKDOWN_NOT_ALLOWED_ERROR,
    SCENE_C2C,
    SCENE_CHANNEL,
    SCENE_DM,
    SCENE_GROUP,
    QQNativeError,
    QQNativeSender,
    build_ark,
    build_keyboard,
    build_markdown,
    markdown_to_plain,
    normalize_target_id,
    split_session,
)

try:
    import botpy.errors as botpy_errors
except Exception:  # pragma: no cover - 仅在未安装 qq-botpy 时走到
    botpy_errors = None


class RecordingApi:
    """记录调用参数的假 BotAPI。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.raises: dict[str, list[BaseException]] = {}

    def _record(self, method: str, kwargs: dict) -> dict:
        self.calls.append((method, kwargs))
        pending = self.raises.get(method)
        if pending:
            raise pending.pop(0)
        return {"id": "m1"}

    def fail_next(self, method: str, exc: BaseException) -> None:
        self.raises.setdefault(method, []).append(exc)

    async def post_group_message(self, **kwargs) -> dict:
        return self._record("post_group_message", kwargs)

    async def post_c2c_message(self, **kwargs) -> dict:
        return self._record("post_c2c_message", kwargs)

    async def post_message(self, **kwargs) -> dict:
        return self._record("post_message", kwargs)

    async def post_dms(self, **kwargs) -> dict:
        return self._record("post_dms", kwargs)


class BuildMarkdownTests(unittest.TestCase):
    def test_custom_content(self):
        self.assertEqual(build_markdown(content="# 标题"), {"content": "# 标题"})

    def test_template_with_params(self):
        md = build_markdown(template_id="tpl_1", params={"title": "标题", "tags": ["a", "b"]})
        self.assertEqual(md["custom_template_id"], "tpl_1")
        self.assertEqual(
            md["params"],
            [
                {"key": "title", "values": ["标题"]},
                {"key": "tags", "values": ["a", "b"]},
            ],
        )
        self.assertNotIn("content", md)

    def test_requires_something(self):
        with self.assertRaises(QQNativeError):
            build_markdown()


class BuildKeyboardTests(unittest.TestCase):
    def test_template_id(self):
        self.assertEqual(build_keyboard(keyboard_id="123"), {"id": "123"})

    def test_inline_buttons_defaults_and_rows(self):
        keyboard = build_keyboard(
            buttons=[
                {"label": "打卡", "data": "checkin"},
                {"label": "官网", "data": "https://example.com", "type": 0, "style": 1, "row": 1},
            ]
        )
        rows = keyboard["content"]["rows"]
        self.assertEqual(len(rows), 2)
        button = rows[0]["buttons"][0]
        self.assertEqual(button["render_data"]["label"], "打卡")
        self.assertEqual(button["render_data"]["style"], 0)
        self.assertEqual(button["action"]["type"], 2)
        self.assertEqual(button["action"]["permission"]["type"], 2)
        self.assertEqual(button["action"]["data"], "checkin")
        self.assertTrue(button["id"])
        self.assertEqual(rows[1]["buttons"][0]["action"]["type"], 0)

    def test_mutually_exclusive(self):
        with self.assertRaises(QQNativeError):
            build_keyboard(keyboard_id="1", buttons=[{"label": "a"}])

    def test_row_out_of_range(self):
        with self.assertRaises(QQNativeError):
            build_keyboard(buttons=[{"label": "a", "row": 9}])

    def test_too_many_buttons_in_one_row(self):
        with self.assertRaises(QQNativeError):
            build_keyboard(buttons=[{"label": f"b{i}"} for i in range(6)])

    def test_missing_label(self):
        with self.assertRaises(QQNativeError):
            build_keyboard(buttons=[{"data": "x"}])

    def test_empty_returns_none(self):
        self.assertIsNone(build_keyboard())


class BuildArkTests(unittest.TestCase):
    def test_ok(self):
        ark = build_ark(37, [{"key": "title", "value": "标题"}])
        self.assertEqual(ark, {"template_id": 37, "kv": [{"key": "title", "value": "标题"}]})

    def test_numeric_string_template(self):
        self.assertEqual(build_ark("37")["template_id"], 37)

    def test_invalid_template(self):
        with self.assertRaises(QQNativeError):
            build_ark("abc")

    def test_kv_requires_key(self):
        with self.assertRaises(QQNativeError):
            build_ark(1, [{"value": "v"}])


class MarkdownToPlainTests(unittest.TestCase):
    def test_strips_common_markup(self):
        plain = markdown_to_plain("# 标题\n\n**粗体** _斜体_ ~~删除~~\n[官网](https://a.b)\n![图](https://a.c)\n- 项目\n> 引用")
        self.assertNotIn("#", plain)
        self.assertNotIn("**", plain)
        self.assertNotIn("~~", plain)
        self.assertIn("标题", plain)
        self.assertIn("粗体", plain)
        self.assertIn("官网", plain)
        self.assertIn("https://a.b", plain)
        self.assertIn("https://a.c", plain)
        self.assertIn("· 项目", plain)
        self.assertIn("引用", plain)


class SessionHelpersTests(unittest.TestCase):
    def test_split_session(self):
        self.assertEqual(
            split_session("qq_main:GroupMessage:ABC"),
            ("qq_main", "GroupMessage", "ABC"),
        )

    def test_split_session_invalid(self):
        with self.assertRaises(QQNativeError):
            split_session("qq_main:GroupMessage")

    def test_normalize_target_id_group(self):
        # 与 AstrBot 适配器保持一致：GroupMessage 会话取最后一段，
        # 以兼容历史上频道会话的 guild_id_channel_id 复合形态。
        self.assertEqual(normalize_target_id("GUILD_CHANNEL", "GroupMessage"), "CHANNEL")
        self.assertEqual(normalize_target_id("B1A2C3D4E5", "GroupMessage"), "B1A2C3D4E5")
        self.assertEqual(normalize_target_id("A1B2C3D4E5", "FriendMessage"), "A1B2C3D4E5")


class SenderTests(unittest.TestCase):
    def _sender(self, api: RecordingApi, **kwargs) -> QQNativeSender:
        return QQNativeSender(api, **kwargs)

    def test_group_passive_reply(self):
        api = RecordingApi()
        sender = self._sender(api)
        result = asyncio.run(
            sender.send_markdown(
                scene=SCENE_GROUP,
                target_id="G1",
                content="# 标题",
                msg_id="USER_MSG",
            )
        )
        method, payload = api.calls[0]
        self.assertEqual(method, "post_group_message")
        self.assertEqual(payload["group_openid"], "G1")
        self.assertEqual(payload["msg_type"], 2)
        self.assertEqual(payload["markdown"], {"content": "# 标题"})
        self.assertEqual(payload["msg_id"], "USER_MSG")
        self.assertIn("msg_seq", payload)
        self.assertNotIn("keyboard", payload)
        self.assertIn("Markdown", result)

    def test_group_proactive_push_has_seq_without_msg_id(self):
        api = RecordingApi()
        result = asyncio.run(
            self._sender(api).send_markdown(
                scene=SCENE_GROUP,
                target_id="G1",
                content="hi",
                msg_id=None,
            )
        )
        self.assertIn("主动推送", result)
        _, payload = api.calls[0]
        self.assertNotIn("msg_id", payload)
        self.assertIn("msg_seq", payload)

    def test_keyboard_template_attached(self):
        api = RecordingApi()
        asyncio.run(
            self._sender(api).send_markdown(
                scene=SCENE_GROUP,
                target_id="G1",
                content="hi",
                keyboard_id="tpl_btn",
            )
        )
        _, payload = api.calls[0]
        self.assertEqual(payload["keyboard"], {"id": "tpl_btn"})

    def test_c2c_uses_user_endpoint(self):
        api = RecordingApi()
        asyncio.run(self._sender(api).send_markdown(scene=SCENE_C2C, target_id="U1", content="hi"))
        method, payload = api.calls[0]
        self.assertEqual(method, "post_c2c_message")
        self.assertEqual(payload["openid"], "U1")
        self.assertEqual(payload["msg_type"], 2)

    def test_channel_drops_msg_type(self):
        api = RecordingApi()
        asyncio.run(
            self._sender(api).send_markdown(scene=SCENE_CHANNEL, target_id="C1", content="hi")
        )
        method, payload = api.calls[0]
        self.assertEqual(method, "post_message")
        self.assertEqual(payload["channel_id"], "C1")
        self.assertNotIn("msg_type", payload)
        self.assertNotIn("msg_seq", payload)

    def test_dms_endpoint(self):
        api = RecordingApi()
        asyncio.run(self._sender(api).send_markdown(scene=SCENE_DM, target_id="GUILD", content="hi"))
        method, payload = api.calls[0]
        self.assertEqual(method, "post_dms")
        self.assertEqual(payload["guild_id"], "GUILD")

    def test_unsupported_scene(self):
        api = RecordingApi()
        with self.assertRaises(QQNativeError):
            asyncio.run(
                self._sender(api).send_markdown(scene="unknown", target_id="X", content="hi")
            )

    def test_markdown_rejected_falls_back_to_plain(self):
        api = RecordingApi()
        exc = (
            botpy_errors.ServerError(MARKDOWN_NOT_ALLOWED_ERROR)
            if botpy_errors
            else RuntimeError(MARKDOWN_NOT_ALLOWED_ERROR)
        )
        api.fail_next("post_group_message", exc)
        result = asyncio.run(
            self._sender(api).send_markdown(scene=SCENE_GROUP, target_id="G1", content="# 标题")
        )
        self.assertEqual(len(api.calls), 2)
        _, fallback = api.calls[1]
        self.assertEqual(fallback["msg_type"], 0)
        self.assertEqual(fallback["content"], "标题")
        self.assertNotIn("markdown", fallback)
        self.assertIn("Markdown", result)

    def test_markdown_rejected_without_fallback_raises(self):
        api = RecordingApi()
        exc = (
            botpy_errors.ServerError(MARKDOWN_NOT_ALLOWED_ERROR)
            if botpy_errors
            else RuntimeError(MARKDOWN_NOT_ALLOWED_ERROR)
        )
        api.fail_next("post_group_message", exc)
        with self.assertRaises(QQNativeError):
            asyncio.run(
                self._sender(api, markdown_fallback_to_plain=False).send_markdown(
                    scene=SCENE_GROUP, target_id="G1", content="# 标题"
                )
            )

    def test_stale_msg_id_retries_as_proactive(self):
        api = RecordingApi()
        exc = (
            botpy_errors.ForbiddenError("forbidden")
            if botpy_errors
            else RuntimeError("forbidden msg_id")
        )
        api.fail_next("post_group_message", exc)
        asyncio.run(
            self._sender(api).send_markdown(
                scene=SCENE_GROUP, target_id="G1", content="hi", msg_id="OLD"
            )
        )
        self.assertEqual(len(api.calls), 2)
        _, retry = api.calls[1]
        self.assertNotIn("msg_id", retry)
        self.assertEqual(retry["msg_type"], 2)

    def test_send_ark(self):
        api = RecordingApi()
        result = asyncio.run(
            self._sender(api).send_ark(
                scene=SCENE_GROUP,
                target_id="G1",
                template_id=37,
                kv=[{"key": "title", "value": "标题"}],
            )
        )
        method, payload = api.calls[0]
        self.assertEqual(method, "post_group_message")
        self.assertEqual(payload["msg_type"], 3)
        self.assertEqual(payload["ark"]["template_id"], 37)
        self.assertIn("Ark", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
