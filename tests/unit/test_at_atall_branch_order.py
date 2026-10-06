"""复现 At/AtAll 分支顺序导致的死代码问题。

`AtAll` 是 `At` 的子类（`class AtAll(At)`），同一个对象同时满足
`isinstance(x, At)` 与 `isinstance(x, AtAll)`。当代码先判断 `At` 时，
`elif isinstance(x, AtAll)` 分支永远不会执行。

上游已在 5935430b1（fix: correct At/AtAll branch order in message outline
#9993）修过其中一处，但本 fork 的初始导入早于该修复，因此本仓库仍有三处
同类问题，且 tests/unit/test_astr_message_event.py 里的断言把错误行为
写死了（注释写着 "AtAll format is [At:all] in the actual implementation"）。

修复前这些测试会失败。
"""

import pytest

from astrbot.core.message.components import At, AtAll, Plain
from astrbot.core.message.message_event_result import MessageChain  # noqa: F401
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


class _ConcreteEvent(AstrMessageEvent):
    async def send(self, message):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def platform_meta():
    return PlatformMetadata(name="test", description="test", id="test")


def make_event(components, platform_meta):
    """按 tests/unit/test_astr_message_event.py 的方式构造事件。"""
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember

    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot123"
    message.session_id = "session123"
    message.message_id = "msg123"
    message.sender = MessageMember(user_id="user123", nickname="tester")
    message.message = components
    message.message_str = "hi"
    message.raw_message = None

    return _ConcreteEvent(
        message_str="hi",
        message_obj=message,
        platform_meta=platform_meta,
        session_id="session123",
    )


# ---------------------------------------------------------------- 前提


def test_atall_is_subclass_of_at():
    """这是全部三处 bug 的根因，先钉死前提。"""
    assert issubclass(AtAll, At)

    instance = AtAll()
    assert isinstance(instance, At)
    assert isinstance(instance, AtAll)
    assert instance.qq == "all"


# ------------------------------------------------- 1. get_message_outline()


def test_outline_renders_at_all_as_quan_ti(platform_meta):
    """@全体成员 应渲染为 [At:全体成员]，而不是 [At:all]。"""
    event = make_event([AtAll()], platform_meta)

    outline = event.get_message_outline()

    assert outline == "[At:全体成员]", (
        f"AtAll 落到了 At 分支：{outline!r}；说明 elif isinstance(i, AtAll) 是死代码"
    )


def test_outline_still_renders_plain_at(platform_meta):
    """普通 @某人 必须不受影响。"""
    event = make_event([At(qq="12345")], platform_meta)

    assert event.get_message_outline() == "[At:12345]"


def test_outline_mixed_at_and_at_all(platform_meta):
    """混合场景：两者必须都能正确渲染。

    parts 以 " ".join 拼接，Plain(" ") 自身再贡献一段空白，因此这里是 3 个空格。
    """
    event = make_event([At(qq="1"), Plain(text=" "), AtAll()], platform_meta)

    assert event.get_message_outline() == "[At:1]   [At:全体成员]"


# ---------------------------------- 2. quoted_message/chain_parser.py


def test_quoted_chain_renders_at_all_without_none():
    """引用消息里的 @全体成员 不应落到 At 分支取到 name/qq 的意外值。"""
    from astrbot.core.utils.quoted_message.chain_parser import (
        _extract_text_from_component_chain,
    )

    text = _extract_text_from_component_chain([Plain(text="看这个"), AtAll()])

    assert "看这个" in text
    assert "None" not in text, f"@全体 落到 At 分支并取到 None：{text!r}"
    assert "全体" in text, f"未走 AtAll 分支：{text!r}"


def test_quoted_chain_still_renders_plain_at_name():
    """普通 @ 仍优先用 name，其次 qq。"""
    from astrbot.core.utils.quoted_message.chain_parser import (
        _extract_text_from_component_chain,
    )

    assert _extract_text_from_component_chain([At(qq="1", name="小明")]) == "@小明"
    assert _extract_text_from_component_chain([At(qq="1", name="")]) == "@1"


# ------------------------------ 3. group_chat_context 的描述顺序


def test_group_chat_context_checks_atall_before_at():
    """群聊上下文里 AtAll 分支也必须排在 At 之前。"""
    import inspect

    from astrbot.builtin_stars.astrbot import group_chat_context

    src = inspect.getsource(group_chat_context)
    at_pos = src.find("isinstance(c, At)")
    atall_pos = src.find("isinstance(c, AtAll)")

    assert at_pos != -1 and atall_pos != -1, "未找到两个分支"
    assert atall_pos < at_pos, (
        "AtAll 分支排在 At 之后，永远不会执行；必须把 AtAll 移到 At 之前"
    )
