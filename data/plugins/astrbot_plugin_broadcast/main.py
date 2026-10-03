"""QQ 广播助手插件

为 QQ 平台（NapCat / OneBot v11，以及 QQ 官方机器人）提供广播能力：

1. 定时群发：在配置的时间（24 时制）自动向白名单群聊发送预设内容；
2. 启动通知：机器人启动后，向已知群聊发送预设通知；
3. 指令广播：/广播 {群聊id或群名} {内容} 向指定群聊发送；/广播 {个人id} {内容} 向个人发送。
   仅 AstrBot 管理员可用（admins_id 列表），不受白名单限制。
4. 群聊列表：/群聊列表 查看本地缓存（KV）里记录过的群聊。

官方机器人没有群列表接口，因此各平台各自维护一份本地的群聊缓存（KV），
群里有消息时自动记录（含群名），定时群发/启动通知/指令广播都基于它工作。

作者: 艾珀莉亚 (FionaFaust)
"""

import asyncio
import json
import random
import re
from datetime import datetime

from botpy.http import Route

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_tools import StarTools

# 定时任务检查间隔（秒）
CHECK_INTERVAL = 20
# 等待适配器连接的最长次数（每次间隔 2 秒）
MAX_WAIT_ROUNDS = 60
# QQ 官方机器人平台（群/成员 ID 为 openid 字符串）
OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
# 群聊缓存文件（KV）
CACHE_FILE = "group_cache.json"
# 官方平台 openid 只含这类字符，其它一律当作群名去匹配
OPENID_LIKE = re.compile(r"[0-9A-Za-z_-]+")


class BroadcastPlugin(Star):
    """QQ 广播助手插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sent_today = False  # 当日定时消息是否已发送标记
        self._cache_file = (
            StarTools.get_data_dir("astrbot_plugin_broadcast") / CACHE_FILE
        )
        # {平台实例 ID: {群 ID: {"name": 群名, "seen": 首次记录时间}}}
        self._groups: dict[str, dict[str, dict]] = self._load_cache()

        # 启动定时群发任务
        if self.config.get("enable_scheduled", False):
            asyncio.create_task(self._scheduled_loop())
            logger.info("定时群发任务已启动")

    # ==================== 群聊缓存（KV） ====================

    def _load_cache(self) -> dict:
        """读取群聊缓存"""
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"加载群聊缓存失败: {e}")
            return {}

    def _save_cache(self):
        """写入群聊缓存"""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(self._groups, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存群聊缓存失败: {e}")

    def _remember_group(self, platform_id: str, group_id: str, name: str = "") -> bool:
        """记录群聊，返回是否有变化（只在新增群或群名变化时写盘）"""
        if not platform_id or not group_id:
            return False
        groups = self._groups.setdefault(str(platform_id), {})
        entry = groups.get(str(group_id))
        if entry is None:
            groups[str(group_id)] = {
                "name": name,
                "seen": datetime.now().strftime("%m-%d %H:%M"),
            }
            return True
        if name and entry.get("name") != name:
            entry["name"] = name
            return True
        return False

    def _find_group(self, group_id: str) -> tuple[str, str] | None:
        """在缓存里按群 ID 找平台，返回 (平台实例 ID, 群 ID)"""
        for platform_id, groups in self._groups.items():
            if str(group_id) in groups:
                return platform_id, str(group_id)
        return None

    def _find_group_by_name(self, name: str) -> list[tuple[str, str]]:
        """按群名在缓存里找群，返回 [(平台实例 ID, 群 ID)]（先精确、再忽略大小写）"""
        name = name.strip()
        if not name:
            return []
        for ignore_case in (False, True):
            hits = [
                (platform_id, gid)
                for platform_id, groups in self._groups.items()
                for gid, info in groups.items()
                if (info.get("name") or "")
                and (
                    info["name"].lower() == name.lower()
                    if ignore_case
                    else info["name"] == name
                )
            ]
            if hits:
                return hits
        return []

    @staticmethod
    def _strip_prefix(message_str: str) -> str:
        """去掉指令前缀"""
        msg = message_str.strip()
        for prefix in ("/广播", "/broadcast"):
            if msg.startswith(prefix):
                return msg[len(prefix) :].strip()
        return msg

    def _split_by_group_name(self, message_str: str) -> tuple[str, str] | None:
        """按缓存里的群名把消息切成 (群名, 内容)，群名可含空格；无匹配返回 None"""
        msg = self._strip_prefix(message_str)
        if not msg:
            return None
        # 群名长的优先，避免短名先命中
        names = sorted(
            {
                info["name"]
                for groups in self._groups.values()
                for info in groups.values()
                if info.get("name")
            },
            key=len,
            reverse=True,
        )
        for name in names:
            head = msg[: len(name)]
            if head.lower() == name.lower():  # 忽略大小写
                rest = msg[len(name) :]
                if not rest:
                    return name, ""
                if rest[:1].isspace():
                    return name, rest.strip()
        return None

    # ==================== 平台工具方法 ====================

    def _iter_platforms(self) -> list:
        """获取所有已加载的平台实例"""
        try:
            return list(self.context.platform_manager.platform_insts)
        except Exception as e:
            logger.error(f"获取平台实例失败: {e}")
            return []

    def _get_platform(self, platform_id: str = ""):
        """获取可用的平台实例，未找到返回 None"""
        for inst in self._iter_platforms():
            if platform_id and inst.meta().id != platform_id:
                continue
            try:
                if inst.get_client() is not None:
                    return inst
            except Exception:
                continue
        return None

    @staticmethod
    def _is_official(platform) -> bool:
        return platform.meta().name in OFFICIAL_PLATFORMS

    @staticmethod
    def _build_group_umo(platform_id: str, group_id: str) -> str:
        """构造群聊 unified_msg_origin"""
        return str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, group_id))

    @staticmethod
    def _build_private_umo(platform_id: str, user_id: str) -> str:
        """构造私聊 unified_msg_origin"""
        return str(MessageSession(platform_id, MessageType.FRIEND_MESSAGE, user_id))

    def _get_whitelist(self) -> list:
        """获取定时群发的白名单群聊 ID 列表（统一转为字符串）"""
        raw = self.config.get("scheduled_group_ids", []) or []
        return [str(g).strip() for g in raw if str(g).strip()]

    async def _send_group_text(self, platform, group_id: str, content: str):
        """向群聊发送文本：官方平台走主动消息接口，其余平台走统一会话"""
        if self._is_official(platform):
            client = platform.get_client()
            await client.api.post_group_message(
                group_openid=str(group_id),
                msg_type=0,
                content=content,
                msg_seq=random.randint(1, 10000),
            )
            return
        await self.context.send_message(
            self._build_group_umo(platform.meta().id, str(group_id)),
            MessageChain().message(content),
        )

    async def _fetch_group_name(self, platform, group_id: str) -> str:
        """取群名称：官方平台查群信息接口，其余平台查 OneBot 接口"""
        try:
            client = platform.get_client()
            if self._is_official(platform):
                route = Route("GET", f"/v2/groups/{group_id}/info")
                info = await client.api._http.request(route)
            else:
                info = await client.api.call_action(
                    "get_group_info", group_id=int(group_id)
                )
            return str((info or {}).get("group_name") or "")
        except Exception as e:
            logger.debug(f"获取群 {group_id} 名称失败: {e}")
            return ""

    async def _platform_group_ids(self, platform) -> list[str]:
        """取平台下的群聊：官方平台用本地缓存，其余平台查群列表接口"""
        platform_id = platform.meta().id
        if self._is_official(platform):
            return list(self._groups.get(platform_id, {}))

        try:
            client = platform.get_client()
            groups = await client.api.call_action("get_group_list") or []
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
            return []

        gids = []
        for group in groups:
            gid = group.get("group_id")
            if gid is None:
                continue
            gids.append(str(gid))
            if self._remember_group(platform_id, str(gid), group.get("group_name", "")):
                self._save_cache()
        return gids

    # ==================== 群聊缓存记录 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember_group(self, event: AstrMessageEvent):
        """群里有消息时把群聊记进缓存（官方平台只能靠这个方式积累）"""
        group_id = event.get_group_id()
        platform_id = event.get_platform_id()
        if not group_id or not platform_id:
            return
        known = self._groups.get(platform_id, {}).get(str(group_id))
        if known and known.get("name"):
            return
        platform = self._get_platform(platform_id)
        name = await self._fetch_group_name(platform, group_id) if platform else ""
        if self._remember_group(platform_id, group_id, name):
            self._save_cache()

    # ==================== 功能一：定时群发 ====================

    async def _scheduled_loop(self):
        """定时任务主循环：每分钟检查是否到达设定时间"""
        while True:
            try:
                if not self.config.get("enable_scheduled", False):
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                now = datetime.now().strftime("%H:%M")
                target = str(self.config.get("scheduled_time", "08:00")).strip()

                if now == target and not self._sent_today:
                    await self._send_scheduled()
                    self._sent_today = True
                elif now != target:
                    # 时间已过/未到，重置当日标记以便次日再次发送
                    self._sent_today = False
            except Exception as e:
                logger.error(f"定时任务异常: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _send_scheduled(self):
        """执行定时群发：向白名单群聊发送预设内容"""
        content = str(self.config.get("scheduled_content", "")).strip()
        whitelist = self._get_whitelist()

        if not content:
            logger.warning("定时发送内容为空，已跳过本次发送")
            return
        if not whitelist:
            logger.warning("白名单群聊列表为空，已跳过本次发送")
            return

        success, failed = 0, 0
        for gid in whitelist:
            # 优先用缓存里记录该群的平台，否则依次尝试已就绪的平台
            hit = self._find_group(gid)
            platforms = [
                p
                for p in self._iter_platforms()
                if self._platform_ready(p) and (not hit or p.meta().id == hit[0])
            ]
            last_error = "未找到可用的平台适配器"
            for platform in platforms:
                try:
                    await self._send_group_text(platform, gid, content)
                except Exception as e:
                    last_error = e
                    continue
                success += 1
                logger.info(f"定时消息已发送到群 {gid}")
                break
            else:
                failed += 1
                logger.error(f"定时消息发送到群 {gid} 失败: {last_error}")
        logger.info(f"定时群发完成: 成功 {success} 个群, 失败 {failed} 个群")

    # ==================== 功能二：启动通知 ====================

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 初始化完成时触发，等待适配器连接后发送启动通知"""
        if not self.config.get("enable_startup", False):
            return
        asyncio.create_task(self._startup_notify())

    async def _startup_notify(self):
        """等待适配器连接成功，然后向所有已知群聊发送启动通知"""
        platforms = []
        # 轮询等待适配器及其客户端就绪
        for _ in range(MAX_WAIT_ROUNDS):
            platforms = [p for p in self._iter_platforms() if self._platform_ready(p)]
            if platforms:
                break
            await asyncio.sleep(2)

        if not platforms:
            logger.error("启动通知: 平台适配器未就绪，跳过发送")
            return

        content = str(self.config.get("startup_content", "机器人已启动")).strip()
        if not content:
            logger.warning("启动通知内容为空，跳过发送")
            return

        for platform in platforms:
            group_ids = await self._platform_group_ids(platform)
            sent = 0
            for gid in group_ids:
                try:
                    await self._send_group_text(platform, gid, content)
                    sent += 1
                except Exception as e:
                    logger.error(f"启动通知发送到群 {gid} 失败: {e}")
            logger.info(
                f"启动通知完成: {platform.meta().name} 已发送到 {sent}/{len(group_ids)} 个群聊"
            )

    def _platform_ready(self, platform) -> bool:
        """平台客户端是否就绪"""
        try:
            return platform.get_client() is not None
        except Exception:
            return False

    # ==================== 功能三：指令广播 ====================

    @filter.command("广播")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def broadcast(
        self, event: AstrMessageEvent, target_id: str = "", content: str = ""
    ):
        """广播指令（需要 AstrBot 管理员权限）

        用法:
          /广播 {群聊id|群名} {内容}  —— 向指定群聊发送消息（官方平台填 group_openid，不受白名单限制）
          /广播 {个人id} {内容}       —— 向指定个人发送消息（OneBot 平台）
        """
        # 参数兜底：若自动解析不完整，则手动解析消息文本
        if not target_id or not content:
            parts = self._strip_prefix(event.message_str).split(maxsplit=1)
            if len(parts) < 2:
                yield event.plain_result("用法: /广播 {群聊id|群名或个人id} {内容}")
                return
            target_id, content = parts[0].strip(), parts[1].strip()

        if not content:
            yield event.plain_result("广播内容不能为空")
            return

        # 先按群名匹配（群名可含空格，从原始消息里切更准）
        hit = None
        if split := self._split_by_group_name(event.message_str):
            name, rest = split
            hits = self._find_group_by_name(name)
            if len(hits) > 1:
                gid_text = "、".join(gid for _, gid in hits)
                yield event.plain_result(
                    f"匹配到 {len(hits)} 个名为「{name}」的群，请改用群 ID：{gid_text}"
                )
                return
            if hits:
                hit = hits[0]
                content = rest

        # 目标所在平台：优先缓存里记录过的平台，其次指令来源平台
        target_id = hit[1] if hit else target_id
        platform_id = hit[0] if hit else event.get_platform_id()
        platform = self._get_platform(platform_id)
        if platform is None:
            yield event.plain_result("错误: 未找到可用的平台适配器")
            return

        if (
            self._is_official(platform)
            and not hit
            and not OPENID_LIKE.fullmatch(target_id)
        ):
            yield event.plain_result(
                f"未找到群名为「{target_id}」的群；官方平台请填 group_openid（可用 /群聊列表 查看）"
            )
            return

        is_group = bool(hit) or self._is_official(platform)
        if not is_group:
            if not target_id.isdigit():
                yield event.plain_result(
                    f"未找到群名为「{target_id}」的群；个人 ID 需为数字"
                )
                return
            # OneBot 平台：先用群信息接口判断是不是群聊
            try:
                client = platform.get_client()
                await client.api.call_action("get_group_info", group_id=int(target_id))
                is_group = True
            except Exception:
                is_group = False

        try:
            if is_group:
                # 群聊目标：直接发送（白名单只限制定时群发）
                await self._send_group_text(platform, target_id, content)
                target_desc = f"群 {target_id}"
            else:
                # 个人目标：直接私聊发送
                await self.context.send_message(
                    self._build_private_umo(platform.meta().id, target_id),
                    MessageChain().message(content),
                )
                target_desc = f"个人 {target_id}"

            logger.info(f"广播成功: {content} -> {target_desc}")
            yield event.plain_result(f"✅ 已发送到{target_desc}: {content}")
        except Exception as e:
            logger.error(f"广播失败: {e}")
            yield event.plain_result(f"广播失败: {e}")

    # ==================== 功能四：群聊列表 ====================

    @filter.command("群聊列表", alias={"群列表", "广播群列表"})
    async def list_groups(self, event: AstrMessageEvent):
        """查看本地缓存（KV）里的群聊列表，含群名"""
        if not self._groups:
            yield event.plain_result(
                "【群聊列表】暂无记录：群里有消息后会自动记录，也可查看插件数据目录的 group_cache.json"
            )
            return

        lines: list[str] = []
        total = 0
        for platform_id, groups in self._groups.items():
            platform = self._get_platform(platform_id)
            label = f"{platform.meta().name}:{platform_id}" if platform else platform_id
            for gid, info in groups.items():
                total += 1
                # 顺手刷新群名，群改名后列表不会一直显示旧名
                if platform is not None:
                    if name := await self._fetch_group_name(platform, gid):
                        if self._remember_group(platform_id, gid, name):
                            self._save_cache()
                name = info.get("name") or "（未知群名）"
                lines.append(f"[{label}] {name} ｜ {gid} ｜ {info.get('seen', '')}")

        lines.sort()
        yield event.plain_result(
            f"【群聊列表】共 {total} 个群（群名 ｜ 群ID ｜ 记录时间）\n"
            + "\n".join(lines)
        )

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件被卸载/停用时调用"""
        self._save_cache()
        logger.info("QQ 广播助手插件已卸载")
