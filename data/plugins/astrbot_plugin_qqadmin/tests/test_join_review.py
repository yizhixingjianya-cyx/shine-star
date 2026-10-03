"""官方平台入群审核测试：轮询申请、自动审批与人工批准/驳回

官方平台不下发入群申请事件（intent 1<<24 未订阅），
因此改用 join_request_list / approval_join_request 轮询实现。
"""

import asyncio
import logging

from astrbot.api.message_components import Reply

from astrbot_plugin_qqadmin.core.join_handle import JoinHandle

from fakes import (
    GROUP_ID,
    SENDER_ID,
    TARGET_ID,
    FakeConfig,
    FakeDB,
    FakeEvent,
    FakePlatformInst,
    FakePlugin,
    OfficialBot,
    OneBotClient,
)

JOIN_REQUEST = {
    "join_request_id": "AeabkHUS5gBlWPfMbhFrnrvW26pvMQD9iHyD33PgZtxu",
    "risk_tips": "",
    "union_openid": "EB4E842FE856B5073A152CC9277FD1B6",
    "member_openid": TARGET_ID,
    "username": "小明",
    "apply_at": "2026-09-27T21:00:00+08:00",
    "apply_source": "self_apply",
    "verify_info": {"method": "verify_message", "verify_message": "暗号"},
}


def run(coro):
    return asyncio.run(coro)


def build_handle(values: dict, requests: list | None = None):
    client = OfficialBot({"/join_request_list": {"list": requests or []}})
    plugin = FakePlugin([FakePlatformInst("qq_official", client)])
    handle = JoinHandle(plugin, FakeConfig(), FakeDB(values=values))
    return handle, client


def approval_calls(client) -> list:
    return [call for call in client.calls if "approval_join_request" in call.path]


def test_official_join_approve_by_accept_word():
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["暗号"]}, [dict(JOIN_REQUEST)]
    )

    run(handle.check_official_requests())

    (call,) = approval_calls(client)
    assert call.method == "POST"
    assert call.path == f"/v2/groups/{GROUP_ID}/approval_join_request/{TARGET_ID}"
    assert call.json == {
        "op": "approve",
        "join_request_id": JOIN_REQUEST["join_request_id"],
    }
    assert client.posts and "自动批准：命中进群白词" in client.posts[0]["content"]
    assert client.posts[0]["group_openid"] == GROUP_ID


def test_official_join_reject_by_reject_word():
    handle, client = build_handle(
        {"join_switch": True, "join_reject_words": ["广告"]}, [dict(JOIN_REQUEST)]
    )
    request = dict(JOIN_REQUEST)
    request["verify_info"] = {"method": "verify_message", "verify_message": "广告"}

    client.http.responses["/join_request_list"] = {"list": [request]}
    run(handle.check_official_requests())

    (call,) = approval_calls(client)
    assert call.json["op"] == "decline"
    assert "黑词" in call.json["reject_reason"]
    assert "add_to_member_blacklist" not in call.json


def test_official_join_reject_adds_platform_blacklist():
    handle, client = build_handle(
        {"join_switch": True, "block_ids": [TARGET_ID], "join_max_time": 0},
        [dict(JOIN_REQUEST)],
    )

    run(handle.check_official_requests())

    (call,) = approval_calls(client)
    assert call.json["op"] == "decline"
    assert call.json["reject_reason"] == "黑名单用户"
    assert call.json["add_to_member_blacklist"] is True
    assert client.posts == []


def test_official_join_manual_review_notifies_once():
    handle, client = build_handle(
        {"join_switch": True, "join_no_match_reject": False}, [dict(JOIN_REQUEST)]
    )

    run(handle.check_official_requests())
    run(handle.check_official_requests())

    assert approval_calls(client) == []
    assert len(client.posts) == 1
    notice = client.posts[0]["content"]
    assert notice.startswith("【进群申请】批准/驳回：")
    assert f"openid：{TARGET_ID}" in notice
    assert f"flag：{JOIN_REQUEST['join_request_id']}" in notice
    assert "暗号" in notice
    # 同一申请只评估一次，避免刷屏与误触进群次数上限
    assert handle._fail == {f"{GROUP_ID}_{TARGET_ID}": 1}


def test_official_join_review_skips_disabled_group():
    handle, client = build_handle({"join_switch": False}, [dict(JOIN_REQUEST)])

    run(handle.check_official_requests())

    assert client.calls == []
    assert client.posts == []


def test_official_join_review_skips_other_platforms():
    onebot = OneBotClient()
    plugin = FakePlugin([FakePlatformInst("aiocqhttp", onebot)])
    handle = JoinHandle(plugin, FakeConfig(), FakeDB(values={"join_switch": True}))

    run(handle.check_official_requests())

    assert onebot.calls == []


def test_official_admin_audit_stays_silent():
    client = OfficialBot({"/join_request_list": {"list": [dict(JOIN_REQUEST)]}})
    plugin = FakePlugin([FakePlatformInst("qq_official", client)])
    handle = JoinHandle(
        plugin,
        FakeConfig(admin_audit=True),
        FakeDB(values={"join_switch": True, "join_max_time": 0}),
    )

    run(handle.check_official_requests())

    assert client.posts == []


OFFICIAL_NOTICE = (
    "【进群申请】批准/驳回：\n"
    "昵称：小明\n"
    f"openid：{TARGET_ID}\n"
    f"flag：{JOIN_REQUEST['join_request_id']}"
)


def test_official_manual_approve_by_quoted_notice():
    client = OfficialBot()
    event = FakeEvent(
        bot=client, message=[Reply(id="MSG_OPENID", message_str=OFFICIAL_NOTICE)]
    )
    handle = JoinHandle(FakePlugin(), FakeConfig(), FakeDB())

    assert run(handle.set_approve(event, approve=True)) == "已同意小明进群"

    (call,) = approval_calls(client)
    assert call.path == f"/v2/groups/{GROUP_ID}/approval_join_request/{TARGET_ID}"
    assert call.json == {
        "op": "approve",
        "join_request_id": JOIN_REQUEST["join_request_id"],
    }


def test_official_manual_reject_by_quoted_notice():
    client = OfficialBot()
    request = Reply(id="MSG_OPENID", message_str=OFFICIAL_NOTICE)
    event = FakeEvent(bot=client, message=[request])
    handle = JoinHandle(FakePlugin(), FakeConfig(), FakeDB())

    reply = run(handle.set_approve(event, extra="答案错误", approve=False))

    assert reply == "已拒绝小明进群\n理由：答案错误"
    (call,) = approval_calls(client)
    assert call.json == {
        "op": "decline",
        "join_request_id": JOIN_REQUEST["join_request_id"],
        "reject_reason": "答案错误",
    }


def test_onebot_manual_approve_keeps_flag_path():
    client = OneBotClient()
    request = Reply(id="10086", message_str=OFFICIAL_NOTICE)
    event = FakeEvent(
        platform="aiocqhttp",
        group_id="123456",
        sender_id="654321",
        bot=client,
        message=[request],
    )
    handle = JoinHandle(FakePlugin(), FakeConfig(), FakeDB())

    assert run(handle.set_approve(event, approve=True)) == "已同意小明进群"

    assert client.called("set_group_add_request") == [
        {
            "flag": JOIN_REQUEST["join_request_id"],
            "sub_type": "add",
            "approve": True,
            "reason": "",
        }
    ]
    assert client.called("get_group_msg_history") == []


def official_read_event(event_name: str, data: dict, client) -> FakeEvent:
    """官方平台下发的群成员事件（正文为空）"""
    return FakeEvent(bot=client, raw_data=data, event_name=event_name)


def test_official_join_request_event_reviews_in_real_time():
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["暗号"]}, [dict(JOIN_REQUEST)]
    )
    event = official_read_event("GROUP_JOIN_REQUEST", dict(JOIN_REQUEST), client)

    run(handle.event_monitoring(event))

    (call,) = approval_calls(client)
    assert call.json["op"] == "approve"
    assert client.posts and "自动批准：命中进群白词" in client.posts[0]["content"]

    # 同一条申请不会再被轮询重复处理
    run(handle.check_official_requests())
    assert len(approval_calls(client)) == 1
    assert len(client.posts) == 1


def test_official_join_request_event_skipped_when_review_off():
    handle, client = build_handle({"join_switch": False}, [])
    event = official_read_event("GROUP_JOIN_REQUEST", dict(JOIN_REQUEST), client)

    run(handle.event_monitoring(event))

    assert client.calls == []
    assert client.posts == []


def test_official_member_add_welcomes_and_bans():
    handle, client = build_handle(
        {"join_welcome": "欢迎 {nickname} 进群", "join_ban_time": 60}
    )
    event = official_read_event(
        "GROUP_MEMBER_ADD", {"group_openid": GROUP_ID, "member_openid": TARGET_ID}, client
    )

    run(handle.event_monitoring(event))

    assert client.posts[0]["content"] == f"欢迎 {TARGET_ID} 进群"
    # 昵称取不到时先查成员接口，再发起进群禁言
    member_info, call = client.calls
    assert member_info.path == f"/v2/groups/{GROUP_ID}/members/{TARGET_ID}"
    assert call.path == f"/v2/groups/{GROUP_ID}/restrict_chat_setting"
    (member,) = call.json["members"]
    assert member["op"] == "add"
    assert member["member_openid"] == TARGET_ID


def test_official_member_remove_notifies_and_blocks():
    handle, client = build_handle({"leave_notify": True, "leave_block": True})
    event = official_read_event(
        "GROUP_MEMBER_REMOVE",
        {"group_openid": GROUP_ID, "member_openid": TARGET_ID},
        client,
    )

    run(handle.event_monitoring(event))

    assert client.posts[0]["content"] == f"{TARGET_ID}({TARGET_ID}) 退出了本群，已拉黑"
    assert handle.db.values["block_ids"] == [TARGET_ID]


def test_official_bot_join_leave_is_logged_only():
    handle, client = build_handle({})

    for event_name in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
        run(
            handle.event_monitoring(
                official_read_event(event_name, {"group_openid": GROUP_ID}, client)
            )
        )

    assert client.calls == []
    assert client.posts == []


def test_official_member_add_ignores_bot_itself():
    handle, client = build_handle({"join_welcome": "欢迎 {nickname}", "join_ban_time": 60})
    event = FakeEvent(
        bot=client,
        self_id=TARGET_ID,
        raw_data={"group_openid": GROUP_ID, "member_openid": TARGET_ID},
        event_name="GROUP_MEMBER_ADD",
    )

    run(handle.event_monitoring(event))

    assert client.posts == []
    assert client.calls == []


def test_onebot_notice_monitoring_keeps_behaviour():
    client = OneBotClient()
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "group_id": "123456",
        "user_id": "654321",
    }
    event = FakeEvent(
        platform="aiocqhttp",
        group_id="123456",
        sender_id="654321",
        bot=client,
        raw_message=raw,
    )
    handle = JoinHandle(
        FakePlugin(),
        FakeConfig(),
        FakeDB(values={"join_welcome": "欢迎 {nickname}", "join_ban_time": 0}),
    )

    run(handle.event_monitoring(event))

    assert event.sent_texts == ["欢迎 654321"]
    assert client.called("set_group_ban") == []


def decision_logs(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "[进群审核]" in record.getMessage()
    ]


def test_join_decision_logged_for_accept_word(caplog):
    handle, _ = build_handle({"join_accept_words": ["暗号"]})

    with caplog.at_level(logging.INFO):
        result = run(handle.should_approve(GROUP_ID, TARGET_ID, "暗号"))

    assert result == (True, "命中进群白词")
    (line,) = decision_logs(caplog)
    assert "黑名单=未命中" in line
    assert "黑词=未命中" in line
    assert "白词=命中(暗号)" in line
    assert line.endswith("→ 批准（命中进群白词）")
    assert GROUP_ID in line and TARGET_ID in line


def test_join_decision_logged_for_manual_review(caplog):
    handle, _ = build_handle({})

    with caplog.at_level(logging.INFO):
        result = run(handle.should_approve(GROUP_ID, TARGET_ID, "随便"))

    assert result == (None, "人工审核")
    (line,) = decision_logs(caplog)
    assert "次数=1/3" in line
    assert "未命中驳回=关" in line
    assert line.endswith("→ 人工审核")


def test_join_decision_logged_for_blocked_user(caplog):
    handle, _ = build_handle({"block_ids": [TARGET_ID]})

    with caplog.at_level(logging.INFO):
        result = run(handle.should_approve(GROUP_ID, TARGET_ID, "暗号"))

    assert result == (False, "黑名单用户")
    (line,) = decision_logs(caplog)
    assert line == f"[进群审核]群{GROUP_ID} 用户{TARGET_ID} 黑名单=命中 → 驳回（黑名单用户）"


def test_official_join_request_logs_received_and_decision(caplog):
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["暗号"]}, [dict(JOIN_REQUEST)]
    )
    event = official_read_event("GROUP_JOIN_REQUEST", dict(JOIN_REQUEST), client)

    with caplog.at_level(logging.INFO):
        run(handle.event_monitoring(event))
        run(handle.check_official_requests())

    logs = decision_logs(caplog)
    assert len(logs) == 2
    assert logs[0] == (
        f"[进群审核]群{GROUP_ID} 收到入群申请：小明({TARGET_ID}) 验证信息=暗号"
    )
    assert logs[1].endswith("→ 批准（命中进群白词）")


def test_join_decision_logged_when_fail_limit_reached(caplog):
    handle, _ = build_handle({"join_max_time": 2})

    with caplog.at_level(logging.INFO):
        first = run(handle.should_approve(GROUP_ID, TARGET_ID, "暗号"))
        second = run(handle.should_approve(GROUP_ID, TARGET_ID, "暗号"))

    assert first == (None, "人工审核")
    assert second == (False, "进群尝试次数已达上限(2次)，已拉黑")
    assert handle.db.values["block_ids"] == [TARGET_ID]
    assert "次数=2/2" in decision_logs(caplog)[-1]
    assert decision_logs(caplog)[-1].endswith("→ 驳回（进群尝试次数已达上限(2次)，已拉黑）")


def test_official_join_request_logged_when_review_off(caplog):
    handle, client = build_handle({"join_switch": False})

    with caplog.at_level(logging.INFO):
        run(
            handle.event_monitoring(
                official_read_event(
                    "GROUP_JOIN_REQUEST", dict(JOIN_REQUEST), client
                )
            )
        )

    (line,) = decision_logs(caplog)
    assert "但本群未开启进群审核，已忽略" in line
    assert "小明" in line
    assert client.calls == []


def test_official_state_log_reports_polling_and_events(monkeypatch, caplog):
    monkeypatch.setattr(
        "astrbot_plugin_qqadmin.core.join_handle.is_installed", lambda: False
    )
    client = OfficialBot()
    plugin = FakePlugin([FakePlatformInst("qq_official", client)])
    handle = JoinHandle(
        plugin, FakeConfig(), FakeDB(values={"join_switch": True})
    )

    with caplog.at_level(logging.INFO):
        run(handle.log_official_state())

    logs = decision_logs(caplog)
    assert f"已开启进群审核的群：['{GROUP_ID}']" in logs[0]
    assert "注入未生效" in logs[1]


def test_official_state_log_without_enabled_group(caplog):
    client = OfficialBot()
    plugin = FakePlugin([FakePlatformInst("qq_official", client)])
    handle = JoinHandle(plugin, FakeConfig(), FakeDB(values={"join_switch": False}))

    with caplog.at_level(logging.INFO):
        run(handle.log_official_state())

    logs = decision_logs(caplog)
    assert logs[0].endswith("（请在群内发送 /进群审核 开）")


def test_official_state_log_skipped_without_official_platform(caplog):
    handle = JoinHandle(
        FakePlugin([FakePlatformInst("aiocqhttp", OneBotClient())]),
        FakeConfig(),
        FakeDB(),
    )

    with caplog.at_level(logging.INFO):
        run(handle.log_official_state())

    assert decision_logs(caplog) == []


def test_join_dedupe_ignores_unstable_request_id():
    """官方 join_request_id 每次拉取都可能变化，同一批待处理申请只能评估一次"""
    handle, client = build_handle(
        {"join_switch": True, "join_no_match_reject": False}, []
    )
    first = dict(JOIN_REQUEST)
    second = dict(JOIN_REQUEST, join_request_id="ANOTHER-TOKEN-0001")
    client.http.responses["/join_request_list"] = {"list": [first]}
    run(handle.check_official_requests())
    client.http.responses["/join_request_list"] = {"list": [second]}
    run(handle.check_official_requests())

    assert handle._fail == {f"{GROUP_ID}_{TARGET_ID}": 1}
    assert len(client.posts) == 1


def test_join_reapply_counts_again_after_request_gone():
    handle, client = build_handle(
        {"join_switch": True, "join_no_match_reject": False}, [dict(JOIN_REQUEST)]
    )
    run(handle.check_official_requests())  # 第 1 次申请
    client.http.responses["/join_request_list"] = {"list": []}
    run(handle.check_official_requests())  # 申请消失（被处理/撤回）
    client.http.responses["/join_request_list"] = {"list": [dict(JOIN_REQUEST)]}
    run(handle.check_official_requests())  # 重新申请

    assert handle._fail == {f"{GROUP_ID}_{TARGET_ID}": 2}
    assert len(client.posts) == 2


def test_official_approve_resets_fail_counter():
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["暗号"]}, [dict(JOIN_REQUEST)]
    )
    handle._fail[f"{GROUP_ID}_{TARGET_ID}"] = 2

    run(handle.check_official_requests())

    assert handle._fail == {}
    (call,) = approval_calls(client)
    assert call.json["op"] == "approve"


def test_join_reject_word_block_adds_block_ids():
    handle, client = build_handle(
        {"join_switch": True, "join_reject_words": ["广告"], "reject_word_block": True},
        [dict(JOIN_REQUEST)],
    )
    request = dict(JOIN_REQUEST)
    request["verify_info"] = {"method": "verify_message", "verify_message": "广告"}
    client.http.responses["/join_request_list"] = {"list": [request]}

    run(handle.check_official_requests())

    assert handle.db.values["block_ids"] == [TARGET_ID]
    (call,) = approval_calls(client)
    assert call.json["op"] == "decline"
    assert call.json["add_to_member_blacklist"] is True


def test_join_accept_word_matches_qa_answer():
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["暗号"]}, [dict(JOIN_REQUEST)]
    )
    request = dict(JOIN_REQUEST)
    request["verify_info"] = {
        "method": "admin_review_qa",
        "review_qa_list": [{"question": "暗号是什么", "answer": "我的暗号"}],
    }
    client.http.responses["/join_request_list"] = {"list": [request]}

    run(handle.check_official_requests())

    (call,) = approval_calls(client)
    assert call.json["op"] == "approve"


def test_join_words_match_case_insensitively():
    handle, client = build_handle(
        {"join_switch": True, "join_accept_words": ["HELLO"]}, [dict(JOIN_REQUEST)]
    )
    request = dict(JOIN_REQUEST)
    request["verify_info"] = {"method": "verify_message", "verify_message": "hello 呀"}
    client.http.responses["/join_request_list"] = {"list": [request]}

    run(handle.check_official_requests())

    (call,) = approval_calls(client)
    assert call.json["op"] == "approve"


def test_block_ids_command_accepts_openids():
    handle, client = build_handle({"block_ids": [TARGET_ID]})

    add_event = official_read_event("GROUP_MEMBER_ADD", {}, client)
    add_event.message_str = f"进群黑名单 +{SENDER_ID}"
    run(handle.handle_block_ids(add_event))
    assert SENDER_ID in handle.db.values["block_ids"]

    del_event = official_read_event("GROUP_MEMBER_ADD", {}, client)
    del_event.message_str = f"进群黑名单 -{TARGET_ID}"
    run(handle.handle_block_ids(del_event))
    assert handle.db.values["block_ids"] == [SENDER_ID]

    overwrite_event = official_read_event("GROUP_MEMBER_ADD", {}, client)
    overwrite_event.message_str = f"进群黑名单 {TARGET_ID}"
    run(handle.handle_block_ids(overwrite_event))
    assert handle.db.values["block_ids"] == [TARGET_ID]


def test_official_state_log_confirms_member_events(monkeypatch, caplog):
    monkeypatch.setattr(
        "astrbot_plugin_qqadmin.core.join_handle.is_installed", lambda: True
    )
    client = OfficialBot()
    plugin = FakePlugin([FakePlatformInst("qq_official", client)])
    handle = JoinHandle(plugin, FakeConfig(), FakeDB(values={"join_switch": True}))

    with caplog.at_level(logging.INFO):
        run(handle.log_official_state())

    logs = decision_logs(caplog)
    assert "已注入 qq-botpy" in logs[1]
    assert "实时生效" in logs[1]


def test_official_join_request_fills_nickname_pool():
    """申请里带的昵称入池，进群欢迎/退群通知即可显示名字"""
    from astrbot_plugin_qqadmin.utils import recall_name

    handle, client = build_handle(
        {"join_switch": True, "join_no_match_reject": False}, [dict(JOIN_REQUEST)]
    )

    run(handle.check_official_requests())

    assert recall_name(GROUP_ID, TARGET_ID) == "小明"
    assert handle.db.nicknames[GROUP_ID] == {TARGET_ID: "小明"}
