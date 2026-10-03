from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..admin_api import OFFICIAL_PLATFORMS, AdminAPI, is_official, official_request
from ..config import PluginConfig
from ..data import QQAdminDB
from ..utils import (
    get_nickname,
    get_reply_message_str,
    parse_bool,
    remember_names,
)
from .official_group_events import is_installed

if TYPE_CHECKING:
    from ..main import QQAdminPlugin

# 官方平台的事件可能丢失或未开通权限，入群申请仍保留轮询兜底（join_request_list 限 30 QPM）
OFFICIAL_REVIEW_INTERVAL = 10


class JoinHandle:
    def __init__(self, plugin: QQAdminPlugin, config: PluginConfig, db: QQAdminDB):
        self.plugin = plugin
        self.cfg = config
        self.db = db
        self._fail: dict[str, int] = {}
        # {群 openid: 本批待处理申请中已评估过的成员 openid}，
        # 官方接口的申请 ID 不稳定，只能按成员去重；申请消失后会允许重新计数
        self._reviewed: dict[str, set[str]] = {}
        self._review_task: asyncio.Task | None = None

    async def initialize(self):
        """启动官方平台的入群申请轮询"""
        if self._review_task is None:
            self._review_task = asyncio.create_task(self.official_join_review())

    async def stop(self):
        """停止轮询"""
        if self._review_task is not None:
            self._review_task.cancel()
            self._review_task = None

    async def log_official_state(self):
        """打印官方平台的入群审核状态，便于定位收不到通知的原因"""
        if not self._official_clients():
            return
        gids = [
            gid
            for gid in self.db.list_group_ids()
            if await self.db.get(gid, "join_switch")
        ]
        logger.info(
            f"[进群审核]官方平台轮询已启动，已开启进群审核的群：{gids}"
            f"{'' if gids else '（请在群内发送 /进群审核 开）'}"
        )
        if self._member_events_enabled():
            logger.info(
                "[进群审核]群成员事件已注入 qq-botpy：入群申请、群成员进退群、"
                "机器人进退群 实时生效"
            )
        else:
            logger.info(
                "[进群审核]群成员事件注入未生效：入群申请靠轮询处理（最长约"
                f" {OFFICIAL_REVIEW_INTERVAL} 秒），"
                "退群通知、进群欢迎、进群禁言 不会生效"
            )

    def _member_events_enabled(self) -> bool:
        """群成员事件注入是否已生效（见 core/official_group_events.py）"""
        return is_installed()

    async def official_join_review(self):
        """轮询官方平台的入群申请并自动审批"""
        while True:
            await asyncio.sleep(OFFICIAL_REVIEW_INTERVAL)
            try:
                await self.check_official_requests()
            except Exception as e:
                logger.error(f"轮询入群申请失败：{e}")

    async def check_official_requests(self):
        """拉取开启了进群审核的群的待处理申请"""
        clients = self._official_clients()
        if not clients:
            return
        gids = [
            gid
            for gid in self.db.list_group_ids()
            if await self.db.get(gid, "join_switch")
        ]
        for client in clients:
            for gid in gids:
                try:
                    data = await official_request(
                        client, gid, "GET", "/join_request_list?limit=50"
                    )
                except Exception as e:
                    logger.error(f"拉取群{gid}的入群申请失败：{e}")
                    continue
                requests = data.get("list") or []
                # 只保留仍在待处理里的成员：申请被处理后重新申请会重新计数
                pending = {str(r.get("member_openid") or "") for r in requests}
                self._reviewed[gid] = {
                    uid for uid in self._reviewed.get(gid, set()) if uid in pending
                }
                for request in requests:
                    await self.review_official_request(client, gid, request)

    async def review_official_request(self, client, gid: str, request: dict):
        """按群配置审批一条入群申请"""
        uid = str(request.get("member_openid") or "")
        if not uid:
            return
        # 申请里带的昵称顺手记进昵称池，后面进群欢迎/退群通知就能显示名字
        if username := str(request.get("username") or ""):
            remember_names(gid, {uid: username})
            await self.db.save_nicknames(gid, {uid: username})
        # 官方接口的 join_request_id 每次拉取都可能变化，按成员去重：
        # 同一批待处理申请只评估一次，避免重复累计进群次数
        reviewed = self._reviewed.setdefault(gid, set())
        if uid in reviewed:
            return
        reviewed.add(uid)

        request_id = str(request.get("join_request_id") or "")
        logger.info(
            f"[进群审核]群{gid} 收到入群申请：{request.get('username') or uid}({uid})"
            f" 验证信息={self._verify_text(request) or '无'}"
        )

        approve, reason = await self.should_approve(
            gid, uid, self._verify_text(request)
        )
        if approve is None:
            await self.notify_official_request(client, gid, request, "")
            return
        if approve:
            # 与 OneBot 侧保持一致：批准后清空该成员的进群尝试次数
            self._fail.pop(f"{gid}_{uid}", None)

        payload = {
            "op": "approve" if approve else "decline",
            "join_request_id": request_id,
        }
        if not approve:
            if reason:
                payload["reject_reason"] = reason
            if uid in await self.db.get(gid, "block_ids", []):
                payload["add_to_member_blacklist"] = True
        try:
            await official_request(
                client, gid, "POST", f"/approval_join_request/{uid}", **payload
            )
        except Exception as e:
            logger.error(f"处理群{gid}的入群申请失败：{e}")
            reviewed.discard(uid)
            return
        if not approve and reason == "黑名单用户":
            return
        await self.notify_official_request(
            client, gid, request, f"自动{'批准' if approve else '驳回'}：{reason}"
        )

    async def notify_official_request(
        self, client, gid: str, request: dict, approve_msg: str
    ):
        """把入群申请通知到群里，便于管理员用「批准/驳回」处理"""
        group_config = self.db.get_group_snapshot(gid)
        if group_config.get("admin_audit", self.cfg.admin_audit):
            # 官方平台无法按 QQ 号私聊管理员，开启 admin_audit 时静默处理
            return
        tip = "批准/驳回：" if not approve_msg else ""
        notice = (
            f"【进群申请】{tip}\n"
            f"昵称：{request.get('username') or request.get('member_openid')}\n"
            f"openid：{request.get('member_openid')}\n"
            f"flag：{request.get('join_request_id')}"
        )
        if risk_tips := request.get("risk_tips"):
            notice += f"\n风险提示：{risk_tips}"
        if verify_text := self._verify_text(request):
            notice += f"\n{verify_text}"
        if approve_msg:
            notice += f"\n{approve_msg}"
        await self.send_official_notice(client, gid, notice)

    async def send_official_notice(self, client, gid: str, text: str):
        """官方平台没有可回复的消息，直接主动发消息到群里"""
        try:
            await client.api.post_group_message(
                group_openid=gid,
                msg_type=0,
                content=text,
                msg_seq=random.randint(1, 10000),
            )
        except Exception as e:
            logger.error(f"发送群{gid}消息失败：{e}")

    @staticmethod
    def _verify_text(request: dict) -> str:
        """取入群申请的验证信息，用于白词/黑词匹配"""
        info = request.get("verify_info") or {}
        if verify_message := info.get("verify_message"):
            return str(verify_message)
        return "\n".join(
            f"{qa.get('question', '')}\n答案：{qa.get('answer', '')}"
            for qa in info.get("review_qa_list") or []
        )

    def _official_clients(self) -> list:
        """获取已加载的官方机器人客户端"""
        clients = []
        for inst in self.plugin.context.platform_manager.platform_insts:
            if inst.meta().name not in OFFICIAL_PLATFORMS:
                continue
            try:
                if client := inst.get_client():
                    clients.append(client)
            except Exception:
                continue
        return clients

    async def _send_admin(self, client: CQHttp, message: str):
        for admin_id in self.cfg.admins_id:
            try:
                await client.send_private_msg(user_id=int(admin_id), message=message)
            except Exception as e:
                logger.error(f"无法发送消息给bot管理员：{e}")

    # -----------修改配置-----------------

    async def handle_join_review(
        self, event: AiocqhttpMessageEvent, mode_str: str | bool | None
    ):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "join_switch", mode)
            await event.send(event.plain_result(f"本群进群审核：{mode}"))
        else:
            status = await self.db.get(gid, "join_switch")
            await event.send(event.plain_result(f"本群进群审核：{status}"))

    async def handle_accept_words(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]
        if raw:
            words = raw.split()
            await self.db.set(gid, "join_accept_words", words)
            await event.send(event.plain_result(f"本群进群白词已设为：{words}"))
        else:
            words = await self.db.get(gid, "join_accept_words", [])
            await event.send(event.plain_result(f"本群进群白词：{words}"))

    async def handle_reject_words(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]
        if raw:
            words = raw.split()
            await self.db.set(gid, "join_reject_words", words)
            await event.send(event.plain_result(f"本群进群黑词已设为：{words}"))
        else:
            words = await self.db.get(gid, "join_reject_words", [])
            await event.send(event.plain_result(f"本群进群黑词：{words}"))

    async def handle_no_match_reject(
        self, event: AiocqhttpMessageEvent, mode_str: str | bool | None
    ):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "join_no_match_reject", mode)
            await event.send(event.plain_result(f"本群未命中白词驳回已设为：{mode}"))
        else:
            status = await self.db.get(gid, "join_no_match_reject")
            await event.send(event.plain_result(f"本群未命中白词驳回：{status}"))

    async def handle_join_max_time(
        self, event: AiocqhttpMessageEvent, time: int | None
    ):
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "join_max_time", time)
            msg = (
                f"本群进群次数已限制为：{time} 次"
                if time > 0
                else "已解除本群的进群次数限制"
            )
            await event.send(event.plain_result(msg))
        else:
            time = await self.db.get(gid, "join_max_time")
            await event.send(event.plain_result(f"本群进群可尝试次数：{time} 次"))

    async def handle_block_ids(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        if not raw:
            ids = await self.db.get(gid, "block_ids", [])
            await event.send(event.plain_result(f"本群进群黑名单：{ids}"))
            return

        if all(not tok.startswith(("+", "-")) for tok in raw.split()):
            new_ids = raw.split()
            await self.db.set(gid, "block_ids", new_ids)
            await event.send(event.plain_result(f"黑名单已覆写为：{' '.join(new_ids)}"))
            return

        curr = set(await self.db.get(gid, "block_ids", []))
        added, removed = [], []
        for tok in raw.split():
            if tok.startswith("+") and len(tok) > 1:
                uid = tok[1:]
                if uid not in curr:
                    curr.add(uid)
                    added.append(uid)
            elif tok.startswith("-") and len(tok) > 1:
                uid = tok[1:]
                if uid in curr:
                    curr.discard(uid)
                    removed.append(uid)

        await self.db.set(gid, "block_ids", list(curr))

        reply = ["本群进群黑名单"]
        if added:
            reply.append(f"新增：{'、'.join(added)}")
        if removed:
            reply.append(f"移除：{'、'.join(removed)}")
        if not added and not removed:
            reply.append("无变动")
        await event.send(event.plain_result("\n".join(reply)))

    async def handle_join_ban(self, event: AiocqhttpMessageEvent, time: int | None):
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "join_ban_time", time)
            msg = f"本群进群禁言已设为：{time} 秒" if time > 0 else "已关闭本群进群禁言"
            await event.send(event.plain_result(msg))
        else:
            t = await self.db.get(gid, "join_ban_time", 0)
            await event.send(event.plain_result(f"本群进群禁言设置：{t} 秒"))

    async def handle_join_welcome(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        if raw:
            await self.db.set(gid, "join_welcome", raw)
            await event.send(event.plain_result(f"本群进群欢迎语已设为：\n{raw}"))
        else:
            text = await self.db.get(gid, "join_welcome", "")
            await event.send(
                event.plain_result(f"本群进群欢迎语：\n{text or '（未设置）'}")
            )

    async def handle_leave_notify(self, event: AiocqhttpMessageEvent, mode_str):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "leave_notify", mode)
            await event.send(event.plain_result(f"本群退群通知已设为：{mode}"))
        else:
            status = await self.db.get(gid, "leave_notify")
            await event.send(event.plain_result(f"本群退群通知：{status}"))

    async def handle_leave_block(self, event: AiocqhttpMessageEvent, mode_str):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "leave_block", mode)
            await event.send(event.plain_result(f"本群退群拉黑已设为：{mode}"))
        else:
            status = await self.db.get(gid, "leave_block")
            await event.send(event.plain_result(f"本群退群拉黑：{status}"))

    # ---------辅助函数-----------------
    async def should_approve(
        self,
        gid: str,
        uid: str,
        comment: str | None = None,
    ) -> tuple[bool | None, str]:
        """判断是否让该用户入群，返回原因"""
        # 本次申请检查过的条件，随判定一起打印，便于排查
        checks: list[str] = []

        # 1.黑名单用户
        block_ids = await self.db.get(gid, "block_ids", [])
        if uid in block_ids:
            checks.append("黑名单=命中")
            return self._log_decision(gid, uid, checks, False, "黑名单用户")
        checks.append("黑名单=未命中")

        if comment:
            # 提取答案部分
            keyword = "\n答案："
            if keyword in comment:
                comment = comment.split(keyword, 1)[1]

            lower_comment = comment.lower()
            # 2.命中进群黑词
            rkws = await self.db.get(gid, "join_reject_words", [])
            reject_hit = next((rk for rk in rkws if rk.lower() in lower_comment), "")
            if reject_hit:
                checks.append(f"黑词=命中({reject_hit})")
                if await self.db.get(gid, "reject_word_block", False):
                    await self.db.add(gid, "block_ids", uid)
                    return self._log_decision(
                        gid, uid, checks, False, "命中进群黑词，已拉黑"
                    )
                return self._log_decision(gid, uid, checks, False, "命中进群黑词")
            checks.append("黑词=未命中")

            # 3.命中进群白词
            akws = await self.db.get(gid, "join_accept_words", [])
            accept_hit = next((ak for ak in akws if ak.lower() in lower_comment), "")
            if akws and accept_hit:
                checks.append(f"白词=命中({accept_hit})")
                return self._log_decision(gid, uid, checks, True, "命中进群白词")
            checks.append("白词=未命中")

        # 4.最大失败次数（考虑到只是防爆破，存内存里足矣，重启清零）
        max_fail = await self.db.get(gid, "join_max_time", 3)
        if max_fail > 0:
            key = f"{gid}_{uid}"
            self._fail[key] = self._fail.get(key, 0) + 1
            checks.append(f"次数={self._fail[key]}/{max_fail}")
            if self._fail[key] >= max_fail:
                await self.db.add(gid, "block_ids", uid)
                return self._log_decision(
                    gid,
                    uid,
                    checks,
                    False,
                    f"进群尝试次数已达上限({max_fail}次)，已拉黑",
                )
        else:
            checks.append("次数=不限")

        # 5.未命中白词时, 自动驳回
        if await self.db.get(gid, "join_no_match_reject"):
            checks.append("未命中驳回=开")
            return self._log_decision(gid, uid, checks, False, "未命中进群关键词")
        checks.append("未命中驳回=关")

        # 6.未命中进群关键词, 人工审核
        return self._log_decision(gid, uid, checks, None, "人工审核")

    @staticmethod
    def _log_decision(
        gid: str,
        uid: str,
        checks: list[str],
        approve: bool | None,
        reason: str,
    ) -> tuple[bool | None, str]:
        """打印入群申请检查过的条件与判定结论"""
        verdict = {True: "批准", False: "驳回", None: "人工审核"}[approve]
        detail = "、".join(checks)
        suffix = "" if reason == verdict else f"（{reason}）"
        logger.info(f"[进群审核]群{gid} 用户{uid} {detail} → {verdict}{suffix}")
        return approve, reason

    # ---------处理事件-----------------

    async def event_monitoring(self, event: AiocqhttpMessageEvent):
        """监听进群/退群事件"""
        raw = getattr(event.message_obj, "raw_message", None)
        if is_official(event):
            await self.official_event_monitoring(event)
            return
        if not isinstance(raw, dict):
            return

        gid: str = str(raw.get("group_id", ""))
        client = event.bot
        uid: str = str(raw.get("user_id", ""))

        # 进群申请事件
        if (
            raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
            and raw.get("sub_type") == "add"
        ):
            # 进群审核总开关
            if not await self.db.get(gid, "join_switch"):
                return
            comment = raw.get("comment")
            flag = raw.get("flag", "")
            info = await client.get_stranger_info(user_id=int(uid))
            nickname = info.get("nickname") or "未知昵称"

            # 判断是否通过
            approve, reason = await self.should_approve(gid, uid, comment)
            # 清理缓存
            if approve is True:
                self._fail.pop(f"{gid}_{uid}", None)

            # 自动审核
            if approve is not None:
                try:
                    await client.set_group_add_request(
                        flag=flag,
                        sub_type="add",
                        approve=approve,
                        reason="" if approve else reason,
                    )
                    if not approve and reason == "黑名单用户":
                        return
                    approve_msg = f"自动{'批准' if approve else '驳回'}：{reason}"
                except Exception as e:
                    logger.warning(f"set_group_add_request failed: {e}")
                    return
            else:
                approve_msg = ""

            # 生成并发送通知
            tip = "批准/驳回：" if not approve_msg else ""
            notice = f"【进群申请】{tip}\n昵称：{nickname}\nQQ：{uid}\nflag：{flag}"
            if comment:
                notice += f"\n{comment}"
            if approve_msg:
                notice += f"\n{approve_msg}"

            group_config = self.db.get_group_snapshot(gid)
            if group_config.get("admin_audit", self.cfg.admin_audit):
                await self._send_admin(client, notice)
            else:
                await event.send(event.plain_result(notice))

        # 主动退群事件
        elif (
            raw.get("post_type") == "notice"
            and raw.get("notice_type") == "group_decrease"
            and raw.get("sub_type") == "leave"
        ):
            if await self.db.get(gid, "leave_notify", False):
                nickname = await get_nickname(event, uid)
                msg = f"{nickname}({uid}) 主动退群了"
                # 退群拉黑
                if await self.db.get(gid, "leave_block", False):
                    await self.db.add(gid, "block_ids", uid)
                    msg += "，已拉黑"
                await event.send(event.plain_result(msg))

        # 进群欢迎、禁言
        elif raw.get("notice_type") == "group_increase" and uid != event.get_self_id():
            # 进群欢迎
            join_welcome = await self.db.get(gid, "join_welcome")
            if join_welcome:
                nickname = await get_nickname(event, uid)
                welcome = join_welcome.format(nickname=nickname)
                await event.send(event.plain_result(welcome))
            # 进群禁言
            join_ban_time = await self.db.get(gid, "join_ban_time")
            if join_ban_time > 0:
                try:
                    await client.set_group_ban(
                        group_id=int(gid),
                        user_id=int(uid),
                        duration=join_ban_time,
                    )
                except Exception:
                    pass

    async def official_event_monitoring(self, event: AiocqhttpMessageEvent):
        """处理官方平台下发的群成员事件

        这些事件由 AstrBot 的官方适配器转成消息事件（正文为空，事件名在 raw_message 上）。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        data = getattr(raw, "raw_data", None)
        if not isinstance(data, dict):
            return
        event_name = str(getattr(raw, "event_name", ""))
        gid = str(data.get("group_openid") or event.get_group_id())
        uid = str(data.get("member_openid") or "")

        # 入群申请：命中规则直接审批，否则通知到群里等人工处理
        if event_name == "GROUP_JOIN_REQUEST":
            if not await self.db.get(gid, "join_switch"):
                logger.info(
                    f"[进群审核]群{gid} 收到 {data.get('username') or uid} 的入群申请，"
                    "但本群未开启进群审核，已忽略（可在群内发送 /进群审核 开）"
                )
                return
            await self.review_official_request(event.bot, gid, data)
            return

        # 群成员加入：进群欢迎 + 进群禁言
        if event_name == "GROUP_MEMBER_ADD" and uid != event.get_self_id():
            if join_welcome := await self.db.get(gid, "join_welcome"):
                nickname = await get_nickname(event, uid)
                await self.send_official_notice(
                    event.bot, gid, join_welcome.format(nickname=nickname)
                )
            join_ban_time = await self.db.get(gid, "join_ban_time")
            if join_ban_time > 0:
                try:
                    await AdminAPI(event).set_group_ban(
                        user_id=uid, duration=join_ban_time
                    )
                except Exception as e:
                    logger.error(f"群{gid}进群禁言失败：{e}")
            return

        # 群成员退出：退群通知 + 退群拉黑
        if event_name == "GROUP_MEMBER_REMOVE":
            if await self.db.get(gid, "leave_notify", False):
                nickname = await get_nickname(event, uid)
                msg = f"{nickname}({uid}) 退出了本群"
                if await self.db.get(gid, "leave_block", False):
                    await self.db.add(gid, "block_ids", uid)
                    msg += "，已拉黑"
                await self.send_official_notice(event.bot, gid, msg)
            return

        # 机器人进退群：仅记录日志
        if event_name in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
            action = "加入" if event_name == "GROUP_ADD_ROBOT" else "退出"
            logger.info(f"官方机器人已{action}群聊 {gid}")

    async def set_approve(
        self, event: AiocqhttpMessageEvent, extra: str = "", approve: bool = True
    ) -> str | None:
        """处理进群申请"""
        text = get_reply_message_str(event)
        if not text:
            return "未引用任何【进群申请】"
        lines = text.split("\n")
        if "【进群申请】" in text and len(lines) >= 4:
            nickname = lines[1].split("：")[1]  # 第2行冒号后文本为nickname
            target = lines[2].split("：")[1]  # 第3行冒号后文本为申请者
            flag = lines[3].split("：")[1]  # 第4行冒号后文本为flag
            try:
                if is_official(event):
                    payload = {
                        "op": "approve" if approve else "decline",
                        "join_request_id": flag,
                    }
                    if not approve and extra:
                        payload["reject_reason"] = extra
                    await official_request(
                        event.bot,
                        event.get_group_id(),
                        "POST",
                        f"/approval_join_request/{target}",
                        **payload,
                    )
                else:
                    await event.bot.set_group_add_request(
                        flag=flag, sub_type="add", approve=approve, reason=extra
                    )
                if approve:
                    reply = f"已同意{nickname}进群"
                else:
                    reply = f"已拒绝{nickname}进群" + (
                        f"\n理由：{extra}" if extra else ""
                    )
                return reply
            except Exception as e:
                logger.error(f"处理进群申请失败: {e}")
                return "这条申请处理过了或者格式不对"

    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """批准进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=True)
        if reply:
            await event.send(event.plain_result(reply))

    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """驳回进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=False)
        if reply:
            await event.send(event.plain_result(reply))
