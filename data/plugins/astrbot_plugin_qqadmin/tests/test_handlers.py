"""群管处理器测试：同一套逻辑在官方机器人与 OneBot 下的行为"""

import asyncio

import pytest

from astrbot.api.message_components import At, Reply

from astrbot_plugin_qqadmin.admin_api import OfficialAPIError, OfficialUnsupportedError
from astrbot_plugin_qqadmin.core import banpro_handel
from astrbot_plugin_qqadmin.core.banpro_handel import BanproHandle
from astrbot_plugin_qqadmin.core.member_handle import MemberHandle
from astrbot_plugin_qqadmin.core.normal_handle import NormalHandle
from astrbot_plugin_qqadmin.core.recall_handel import RecallHandle

from fakes import (
    GROUP_ID,
    SENDER_ID,
    TARGET_ID,
    FakeConfig,
    FakeDB,
    FakeEvent,
    OfficialBot,
    OneBotClient,
)

GROUP_NUM = "123456"
USER_NUM = "654321"
TARGET_NUM = "987654"


def run(coro):
    return asyncio.run(coro)


def official_event(
    message=None,
    message_str="",
    responses=None,
    nickname="小明",
    message_id: str = "10086",
) -> FakeEvent:
    responses = responses or {"/members/": {"username": nickname}}
    return FakeEvent(
        bot=OfficialBot(responses),
        message=message,
        message_str=message_str,
        message_id=message_id,
    )


def onebot_event(message=None, message_str="", responses=None) -> FakeEvent:
    return FakeEvent(
        platform="aiocqhttp",
        group_id=GROUP_NUM,
        sender_id=USER_NUM,
        self_id=USER_NUM,
        message=message,
        message_str=message_str,
        bot=OneBotClient(responses),
    )


def ban_calls(event) -> list:
    """取出官方禁言请求（前面可能还有查昵称的成员信息请求）"""
    return [call for call in event.bot.calls if call.path.endswith("restrict_chat_setting")]


def test_official_set_group_ban():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)])

    result = run(handler.set_group_ban(event))

    assert result == f"用户[小明({TARGET_ID})]已被禁言60秒"
    (member,) = ban_calls(event)[0].json["members"]
    assert member["op"] == "add"
    assert member["member_openid"] == TARGET_ID
    assert event.is_stopped() is True


def test_official_cancel_group_ban():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)])

    result = run(handler.set_group_ban(event, ban_time=0))

    assert result == f"用户[小明({TARGET_ID})]已被禁言0秒"
    (member,) = ban_calls(event)[0].json["members"]
    assert member["op"] == "del"


def test_official_set_group_ban_with_target_id():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event()

    result = run(handler.set_group_ban(event, ban_time=120, target_id=TARGET_ID))

    assert result == f"用户[小明({TARGET_ID})]已被禁言120秒"
    assert ban_calls(event)[0].path == f"/v2/groups/{GROUP_ID}/restrict_chat_setting"


def test_official_set_group_kick():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)])

    result = run(handler.set_group_kick(event))

    assert result == f"已将【{TARGET_ID}-小明】踢出本群"
    nick_call, kick_call = event.bot.calls
    assert nick_call.method == "GET"
    assert kick_call.json == {
        "member_openids": [TARGET_ID],
        "add_to_member_blacklist": False,
    }


def test_official_set_group_block():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)])

    result = run(handler.set_group_block(event))

    assert result == f"已将【{TARGET_ID}-小明】踢出本群并拉黑!"
    assert event.bot.calls[1].json["add_to_member_blacklist"] is True


def test_official_whole_ban_reports_unsupported():
    handler = NormalHandle(FakeConfig(), FakeDB())

    with pytest.raises(OfficialUnsupportedError, match="全员禁言"):
        run(handler.set_group_whole_ban(official_event(), enable=True))


def test_official_word_ban():
    handle = BanproHandle(FakeConfig(), FakeDB(values={"word_ban_time": 300}))
    event = official_event(
        message_str="这是一条 spam 消息", message_id="MSG_OPENID_1"
    )

    hit = run(handle.check_ban_words(event, ["spam"]))

    assert hit is True
    recall, mute = event.bot.calls
    assert recall.method == "DELETE"
    assert recall.path == (
        f"/v2/groups/{GROUP_ID}/messages/{event.message_obj.message_id}"
    )
    (member,) = mute.json["members"]
    assert member["member_openid"] == SENDER_ID
    assert member["op"] == "add"


def test_official_spamming_ban(monkeypatch):
    class FakeTime:
        def __init__(self):
            self._values = iter([1000.0, 1000.1, 1000.2, 1000.3, 1000.4])

        def time(self) -> float:
            return next(self._values)

    monkeypatch.setattr(banpro_handel, "time", FakeTime())
    handle = BanproHandle(FakeConfig(), FakeDB(values={"spamming_ban_time": 60}))
    event = official_event(message=[At(qq=TARGET_ID)], message_str="刷屏")

    for _ in range(5):
        run(handle.spamming_ban(event))

    (member,) = event.bot.calls[0].json["members"]
    assert member["member_openid"] == SENDER_ID
    assert event.sent_texts == ["检测到小明刷屏，已禁言"]


def test_official_recall_quoted_message():
    handler = RecallHandle(FakeConfig(), FakeDB())
    event = official_event(message=[Reply(id="MSG_OPENID_2")])

    run(handler.delete_msg(event))

    call = event.bot.calls[0]
    assert call.method == "DELETE"
    assert call.path == f"/v2/groups/{GROUP_ID}/messages/MSG_OPENID_2"
    assert event.is_stopped() is True


def test_official_batch_recall_reports_unsupported():
    handler = RecallHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)], message_str="撤回 10")

    with pytest.raises(OfficialUnsupportedError, match="批量撤回"):
        run(handler.delete_msg(event))


def test_official_member_list_reports_unsupported():
    handler = MemberHandle(plugin=None)
    event = official_event()

    with pytest.raises(OfficialUnsupportedError, match="群成员列表"):
        run(handler.get_group_member_list(event))
    assert event.sent_texts == ["获取中..."]


def test_official_clear_group_member_degrades_gracefully():
    handler = MemberHandle(plugin=None)
    event = official_event()

    run(handler.clear_group_member(event))

    assert event.sent_texts == ["获取群成员信息失败：官方机器人暂不支持获取群成员列表"]
    assert event.bot.calls == []


def test_onebot_set_group_ban_keeps_original_call():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = onebot_event(message=[At(qq=TARGET_NUM)])

    result = run(handler.set_group_ban(event))

    assert result == f"用户[{TARGET_NUM}]已被禁言60秒"
    assert event.bot.called("set_group_ban") == [
        {
            "group_id": int(GROUP_NUM),
            "user_id": int(TARGET_NUM),
            "duration": 60,
        }
    ]


def test_onebot_whole_ban_keeps_original_call():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = onebot_event()

    result = run(handler.set_group_whole_ban(event, enable=False))

    assert result == "已关闭全体禁言"
    assert event.bot.called("set_group_whole_ban") == [
        {"group_id": int(GROUP_NUM), "enable": False}
    ]


def test_onebot_word_ban_keeps_original_call():
    handle = BanproHandle(FakeConfig(), FakeDB(values={"word_ban_time": 300}))
    event = onebot_event(message_str="这是一条 spam 消息")

    hit = run(handle.check_ban_words(event, ["spam"]))

    assert hit is True
    assert event.bot.called("delete_msg") == [
        {"message_id": int(event.message_obj.message_id)}
    ]
    assert event.bot.called("set_group_ban") == [
        {"group_id": int(GROUP_NUM), "user_id": int(USER_NUM), "duration": 300}
    ]


def test_onebot_recall_quoted_message_keeps_original_call():
    handler = RecallHandle(FakeConfig(), FakeDB())
    event = onebot_event(message=[Reply(id="10086")])

    run(handler.delete_msg(event))

    assert event.bot.called("delete_msg") == [{"message_id": 10086}]


def test_onebot_batch_recall_uses_message_history():
    handler = RecallHandle(FakeConfig(), FakeDB())
    event = onebot_event(message=[At(qq=TARGET_NUM)], message_str="撤回 20")
    event.bot.responses["get_group_msg_history"] = {
        "messages": [
            {"sender": {"user_id": int(TARGET_NUM)}, "message_id": 11},
            {"sender": {"user_id": int(USER_NUM)}, "message_id": 12},
        ]
    }

    run(handler.delete_msg(event))

    assert event.bot.called("get_group_msg_history") == [
        {
            "group_id": int(GROUP_NUM),
            "message_seq": 0,
            "count": 20,
            "reverseOrder": True,
        }
    ]
    assert event.bot.called("delete_msg") == [{"message_id": 11}]
    assert event.sent_texts == ["已从20条消息中撤回1条"]


def official_payload_event(message_str: str) -> FakeEvent:
    """官方群 @ 机器人消息：author 带角色，目标只在 mentions/文本里"""
    return FakeEvent(
        bot=OfficialBot(),
        message_str=message_str,
        raw_data={
            "id": "ROBOT1.0_MSGID",
            "author": {
                "id": SENDER_ID,
                "member_openid": SENDER_ID,
                "member_role": "owner",
                "username": "EmptyLava",
                "bot": False,
            },
            "content": message_str,
            "group_openid": GROUP_ID,
            "message_type": 0,
            "mentions": [
                {
                    "id": TARGET_ID,
                    "member_openid": TARGET_ID,
                    "member_role": "member",
                    "username": "小明",
                    "bot": False,
                }
            ],
        },
    )


def test_official_ban_mentioned_member():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_payload_event(f"/禁言 60 <@{TARGET_ID}>")

    result = run(handler.set_group_ban(event, ban_time=60))

    assert result == f"用户[小明({TARGET_ID})]已被禁言60秒"
    (member,) = ban_calls(event)[0].json["members"]
    assert member["member_openid"] == TARGET_ID
    assert member["op"] == "add"


def test_official_vote_mute_via_payload():
    handle = BanproHandle(FakeConfig(), FakeDB())
    event = official_payload_event(f"/投票禁言 60 <@{TARGET_ID}>")
    handle.vote_cache[GROUP_ID] = {
        "target": TARGET_ID,
        "votes": {},
        "ban_time": 60,
        "expire": 0,
        "threshold": 1,
    }

    run(handle.vote_mute(event, agree=True))

    (member,) = event.bot.calls[0].json["members"]
    assert member["member_openid"] == TARGET_ID
    assert event.sent_texts == ["投票通过！已禁言小明"]


def test_official_kick_reports_api_error():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_payload_event(f"/踢了 <@{TARGET_ID}>")
    event.bot.http.error = RuntimeError("应用无接口访问权限")

    with pytest.raises(OfficialAPIError, match="官方机器人接口调用失败"):
        run(handler.set_group_kick(event))


def test_official_vote_messages_show_name_with_openid():
    """投票禁言的消息里，openid 前补上昵称"""
    from astrbot_plugin_qqadmin.utils import remember_names

    remember_names(GROUP_ID, {TARGET_ID: "小明"})
    handle = BanproHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)], message_str="赞同禁言")
    handle.vote_cache[GROUP_ID] = {
        "target": TARGET_ID,
        "votes": {},
        "ban_time": 60,
        "expire": 0,
        "threshold": 3,
    }

    run(handle.vote_mute(event, agree=False))

    display = f"小明({TARGET_ID})"
    assert event.sent_texts == [f"禁言【{display}】：\n赞同(0/3)\n反对(1/3)"]


def test_official_ban_reply_shows_name_with_openid():
    from astrbot_plugin_qqadmin.utils import remember_names

    remember_names(GROUP_ID, {TARGET_ID: "小明"})
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event()

    result = run(handler.set_group_ban(event, ban_time=60, target_id=TARGET_ID))

    assert result == f"用户[小明({TARGET_ID})]已被禁言60秒"


def test_onebot_ban_reply_keeps_number_only():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = onebot_event()

    result = run(handler.set_group_ban(event, ban_time=60, target_id=TARGET_NUM))

    assert result == f"用户[{TARGET_NUM}]已被禁言60秒"


def test_official_ban_failure_includes_reason():
    """禁言失败时要把官方返回的原因带上，便于排查"""
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = FakeEvent(
        bot=OfficialBot(
            error=RuntimeError("目标成员为机器人/群主/管理员，不允许被禁言")
        )
    )

    result = run(handler.set_group_ban(event, ban_time=60, target_id=TARGET_ID))

    assert result.startswith(f"用户[{TARGET_ID}]禁言失败：")
    assert "不允许被禁言" in result


def test_official_ban_without_duration_ignores_at_text():
    """不带秒数时 @ 出来的文本不能被当成禁言时长"""
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event(message=[At(qq=TARGET_ID)], message_str="禁言 @lumio")

    result = run(handler.set_group_ban(event, ban_time="@lumio"))

    assert result == f"用户[小明({TARGET_ID})]已被禁言60秒"
    (member,) = ban_calls(event)[0].json["members"]
    assert member["member_openid"] == TARGET_ID


def test_onebot_ban_without_duration_ignores_at_text():
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = onebot_event(message=[At(qq=TARGET_NUM)], message_str="禁言 @lumio")

    result = run(handler.set_group_ban(event, ban_time="@lumio"))

    assert result == f"用户[{TARGET_NUM}]已被禁言60秒"
    assert event.bot.called("set_group_ban") == [
        {"group_id": int(GROUP_NUM), "user_id": int(TARGET_NUM), "duration": 60}
    ]


def test_official_ban_accepts_numeric_string_duration():
    """秒数被框架以字符串传入时也要按秒数处理"""
    handler = NormalHandle(FakeConfig(), FakeDB())
    event = official_event()

    result = run(handler.set_group_ban(event, ban_time="600", target_id=TARGET_ID))

    assert result == f"用户[小明({TARGET_ID})]已被禁言600秒"
    (member,) = ban_calls(event)[0].json["members"]
    assert member["op"] == "add"
