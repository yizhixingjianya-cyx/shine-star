"""
emoticon_manager —— AstrBot 情绪表情包插件
================================================
目标平台：AstrBot v4.x（Star 插件体系 / Plugin Pages WebUI）
适配适配器：aiocqhttp（NapCat / OneBot v11 / QQ）

核心功能
--------
0. 自动模式（核心）    —— 机器人完成回复后，按概率随机补发一张表情包
1. 情感匹配（可选）    —— 开启后机器人会只总结本次输出的内容（不附加
                         对话上下文）并判断情感标签，再从标签库中挑选
                         最匹配的表情包（tag 策略）
2. #表情包 / #随机表情  —— 手动发送表情包（不受概率限制）
3. #表情列表 [页码]     —— 分页输出所有表情包文件名
4. #表情删除 文件名     —— 删除指定表情包文件
5. WebUI 管理页面       —— 集成在 AstrBot 插件面板中：
   - 网页直接上传图片（jpg / jpeg / png / gif，单文件最大 8MB）
   - 在线预览所有表情包
   - 网页一键删除表情文件
   - 网页为每张表情打情感标签（tag），并支持按标签筛选

文件存储
--------
表情包统一存放在：<插件目录>/static/emoticons/
插件启动时自动创建目录；仅允许 .jpg / .jpeg / .png / .gif 四种后缀，
其他文件一律过滤。表情标签存放在：<插件目录>/data/tags.json
（纯本地 JSON 元数据文件，不是数据库）。不使用任何第三方数据库，
纯本地文件管理。

⚠️ 关于插件规范版本的重要说明
-----------------------------
AstrBot 自 v3.4.0 起将“插件(Plugin)”更名为“Star”，最新官方规范为：
  - 入口文件必须是 main.py（不再是 __init__.py）
  - 插件类继承 astrbot.api.star.Star（不再是 astrbot.api.plugin.Plugin）
  - 指令使用 @filter.command 装饰器注册（不再是 @command）
  - Web 接口使用 context.register_web_api() + astrbot.api.web
旧版 astrbot.api.plugin.Plugin / @command / __init__.py 入口属于 v2 时代
规范，在当前 AstrBot（v4.x）中已无法加载。本插件严格遵循最新官方规范。
"""

import asyncio
import base64
import json
import math
import os
import random
import re
import threading
import time
import zipfile
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
)

from . import static_host

# =====================================================================
# 常量区
# =====================================================================
PLUGIN_NAME = "emoticon_manager"

# 表情包存储目录：<插件目录>/static/emoticons/
EMOTICONS_DIR = Path(__file__).resolve().parent / "static" / "emoticons"

# 允许的图片后缀（小写）
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif")

# 后缀 -> MIME 类型（用于 Web 接口与预览 data URL）
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

# 上传大小限制：最大 8MB
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# ZIP 压缩包上传限制（压缩包本身仍受 8MB 限制）
MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024  # 解压后所有文件总大小上限（防解压炸弹）
MAX_ZIP_ENTRY_COUNT = 500  # 压缩包内条目数量上限

# 预览阈值：小于等于该字节数的图片在 WebUI 中直接内嵌 base64 预览，
# 更大的图片只显示占位卡片（避免超大 data URL 拖垮页面）。
PREVIEW_MAX_BYTES = 1 * 1024 * 1024

# 聊天指令分页：每页展示的表情数量
PAGE_SIZE = 20

# markdown 表情图前补的一段颜文字（可按喜好修改）
MD_PREFIX_TEXT = "(・∀・)"

# 文件名主干（不含扩展名）最大长度
MAX_STEM_LENGTH = 60

# ---- 表情标签（情感匹配）相关 ----
# 标签数据文件：<插件目录>/data/tags.json
# 结构：{"表情文件名": ["标签1", "标签2", ...]}
TAGS_DATA_DIR = Path(__file__).resolve().parent / "data"
TAGS_DATA_FILE = TAGS_DATA_DIR / "tags.json"

# 单个表情最多允许的标签数量
MAX_TAGS_PER_FILE = 20
# 单个标签最大长度（字符）
MAX_TAG_LENGTH = 20

# 情感分析 LLM 调用超时（秒），防止模型无响应卡死消息流程
EMOTION_LLM_TIMEOUT = 30
# 情感标签结果缓存时间（秒）：同一会话短时间内复用上次结果，
# 避免每条消息都调用一次 LLM，节省 token 与延迟
EMOTION_CACHE_TTL = 60

# 文件系统互斥锁：防止并发的上传/删除操作互相干扰
_FILE_LOCK = threading.Lock()
# 标签数据互斥锁：防止并发的标签读写互相干扰
_TAG_LOCK = threading.Lock()


# =====================================================================
# 文件名安全工具（重点：路径穿越防护）
# =====================================================================
def _safe_filename(filename):
    """清洗外部传入的文件名，返回安全的纯文件名；不合法返回 None。

    防护点：
    1. 去掉一切路径成分（/ 与 \\），只保留 basename；
    2. 拒绝空名、隐藏文件、. 与 ..、NUL 字符；
    3. 只保留 中英文/数字/常见符号，过滤控制字符与危险符号；
    4. 强制扩展名必须是允许的图片后缀；
    5. 拦截 Windows 保留设备名（CON/PRN/AUX/COM1... 等），
       避免在 Windows 上产生设备文件副作用。
    """
    if filename is None:
        return None
    # 统一把反斜杠转成正斜杠再取 basename，天然消除目录穿越
    raw = str(filename).strip().replace("\\", "/")
    if "\x00" in raw:
        return None
    try:
        name = Path(raw).name
    except ValueError:
        # 极少数平台/字符组合会让 Path 构造失败，一律视为非法
        return None
    if not name or name in (".", "..") or name.startswith("."):
        return None
    if "/" in name:
        return None

    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None

    stem = Path(name).stem
    # 仅保留：中英文、数字、下划线、连字符、点、括号、方括号、空格；
    # 空格统一替换为下划线，方便聊天指令 #表情删除 传参。
    stem = re.sub(r"[^\w\u4e00-\u9fff\-_.()（）\[\]【】 ]", "", stem)
    stem = stem.replace(" ", "_")
    stem = stem.strip(" .")
    stem = stem[:MAX_STEM_LENGTH]
    if not stem:
        return None

    # Windows 保留设备名防护
    if stem.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        return None

    return f"{stem}{ext}"


def _resolve_emoticon(filename):
    """把外部文件名解析为表情目录内的真实路径；不合法返回 None。

    双重防护：
    1. 文件名先经过 _safe_filename 清洗，只保留纯文件名；
    2. 对最终路径 resolve() 后校验父目录必须仍是表情目录
       （可拦截符号链接指向目录外的场景）。
    """
    safe = _safe_filename(filename)
    if safe is None:
        return None
    root = EMOTICONS_DIR.resolve()
    target = root / safe
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if resolved.parent != root:
        return None
    return target


def _check_image_magic(head: bytes, ext: str) -> bool:
    """轻量校验图片文件头（魔数），防止任意文件改名伪装成图片上传。"""
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    return False


def _detect_image_type(head: bytes):
    """根据文件头识别图片真实格式，返回规范扩展名（.jpg/.png/.gif），
    无法识别时返回 None（用于自动修正缺失/不规范的文件扩展名）。"""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return None


def _is_zip_head(head: bytes) -> bool:
    """根据文件头判断是否为 ZIP 压缩包（支持空压缩包）。"""
    return head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06")


def _decode_zip_name(name: str) -> str:
    """修复部分 Windows 工具生成的 zip 中文文件名乱码。

    Python zipfile 对未标记 UTF-8 的条目按 cp437 解码，中文会变乱码；
    这里尝试用 cp437 -> gbk 还原。ASCII 名称不受影响。
    """
    if not name:
        return name
    try:
        name.encode("utf-8")
        return name  # 本身就是合法 UTF-8（含纯 ASCII），无需修复
    except UnicodeEncodeError:
        pass
    try:
        fixed = name.encode("cp437").decode("gbk")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _scan_emoticons_sync() -> list:
    """扫描表情目录，返回表情文件信息列表（已过滤非图片文件）。"""
    files = []
    if not EMOTICONS_DIR.is_dir():
        return files
    for entry in EMOTICONS_DIR.iterdir():
        if not entry.is_file():
            continue
        # 过滤隐藏文件 / 临时文件 / 非图片后缀
        if entry.name.startswith(".") or entry.name.startswith("~"):
            continue
        ext = Path(entry.name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        try:
            st = entry.stat()
        except OSError as e:
            logger.error(f"[{PLUGIN_NAME}] 读取文件状态失败 {entry.name}: {e}")
            continue
        files.append(
            {
                "filename": entry.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "content_type": CONTENT_TYPES.get(ext, "application/octet-stream"),
                "preview": None,
                "previewable": False,
            }
        )
    files.sort(key=lambda f: f["filename"].lower())
    return files


def _sanitize_tag(tag) -> str | None:
    """清洗单个情感标签，返回安全的标签字符串；不合法返回 None。

    规则：
    1. 去首尾空白；
    2. 拒绝空串、控制字符、路径分隔符与引号等危险字符；
    3. 只保留 中文/英文/数字/下划线/连字符/括号，长度限制 MAX_TAG_LENGTH。
    """
    if tag is None:
        return None
    raw = str(tag).strip()
    if not raw or len(raw) > MAX_TAG_LENGTH:
        return None
    if "\x00" in raw or "/" in raw or "\\" in raw or '"' in raw or "'" in raw:
        return None
    # 过滤控制字符与无关符号
    clean = re.sub(r"[^\w\u4e00-\u9fff\-（）()【】\[\]]", "", raw)
    clean = clean.strip(" -")
    return clean or None


def _load_tags_sync() -> dict:
    """从 data/tags.json 读取标签数据；文件不存在/损坏时返回空字典。"""
    try:
        if not TAGS_DATA_FILE.is_file():
            return {}
        with TAGS_DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        logger.error(f"[{PLUGIN_NAME}] 读取标签数据失败: {e}")
        return {}

    if not isinstance(raw, dict):
        return {}
    # 规范化：只保留 文件名 -> 合法标签列表
    tags: dict = {}
    for name, tag_list in raw.items():
        if not isinstance(name, str) or not isinstance(tag_list, list):
            continue
        clean_list = []
        for t in tag_list:
            t = _sanitize_tag(t)
            if t and t not in clean_list:
                clean_list.append(t)
            if len(clean_list) >= MAX_TAGS_PER_FILE:
                break
        if clean_list:
            tags[name] = clean_list
    return tags


def _save_tags_sync(tags: dict) -> None:
    """把标签数据原子写入 data/tags.json（先写临时文件再替换）。"""
    try:
        TAGS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError(f"创建标签数据目录失败: {e}") from e

    tmp_path = TAGS_DATA_FILE.with_name(
        f".tags_{int(time.time() * 1000)}_{os.getpid()}.tmp"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(tags, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TAGS_DATA_FILE)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# =====================================================================
# 插件主体（Star 插件体系）
# =====================================================================
class EmoticonManagerPlugin(Star):
    """AstrBot 情绪表情包插件（Star 插件体系）"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # ---- 表情选择策略配置（来自 WebUI 插件设置 _conf_schema.json）----
        cfg = dict(self.config or {})
        # 选择策略：random=随机发送（避免连续重复）；round_robin=轮流发送
        #           tag=情感匹配（总结对话判断情感标签，挑选最匹配的表情包）
        strategy = str(cfg.get("selection_strategy") or "random").strip().lower()
        self.selection_strategy = (
            strategy if strategy in ("random", "round_robin", "tag") else "random"
        )
        # 每次回复最多使用表情数量（1-10）
        self.max_emoticons_per_reply = self._clamp_int(
            cfg.get("max_emoticons_per_reply"),
            1,
            1,
            10,
        )
        # 表情出现概率（0-100，百分比）
        self.emoticon_probability = self._clamp_int(
            cfg.get("emoticon_probability"),
            100,
            0,
            100,
        )
        # 自动发送总开关：机器人回复后是否自动随机补发表情
        self.auto_send_enabled = bool(cfg.get("auto_send_enabled", True))
        # 同一会话两次自动表情的最小间隔（秒），防止一条对话刷出多张
        self.min_interval_seconds = self._clamp_int(
            cfg.get("min_interval_seconds"),
            10,
            0,
            3600,
        )
        # 情感匹配总开关：仅在 selection_strategy=tag 且本开关开启时生效
        self.emotion_match_enabled = bool(cfg.get("emotion_match_enabled", False))
        # 情感标签不依赖配置：自动从表情库（data/tags.json）中收集，
        # 见 _collect_emotion_tags()
        # 情感标签匹配失败时是否回退随机发送（false 则不发）
        self.emotion_fallback_random = bool(cfg.get("emotion_fallback_random", True))

        # ---- markdown 图片发送（本地图床 + 同一条消息）----
        # 总开关：开启后用 markdown 图片语法发送表情（需平台支持 markdown，如 QQ 官方）
        self.md_send_enabled = bool(cfg.get("md_send_enabled", True))
        # 是否把表情拼接到触发它的那条回复消息里（false 则仍单独发一条）
        self.md_attach_to_reply = bool(cfg.get("md_attach_to_reply", True))
        # 本地图床对外地址（如 http://1.2.3.4:11453）；留空则退回普通图片消息
        self.md_image_host_url = str(cfg.get("md_image_host_url") or "").strip()
        self.md_image_host_port = self._clamp_int(
            cfg.get("md_image_host_port"), 11453, 1, 65535
        )
        self.md_image_host_bind = (
            str(cfg.get("md_image_host_bind") or "0.0.0.0").strip() or "0.0.0.0"
        )
        # 显示尺寸上限（只缩小不放大）与最终缩放系数
        self.md_max_width = self._clamp_int(cfg.get("md_max_width"), 240, 16, 4096)
        self.md_max_height = self._clamp_int(cfg.get("md_max_height"), 240, 16, 4096)
        try:
            self.md_scale = float(cfg.get("md_scale") or 1.0)
        except (TypeError, ValueError):
            self.md_scale = 1.0
        if self.md_scale <= 0:
            self.md_scale = 1.0

        # 策略运行状态
        self._rr_index = 0  # 轮询游标（round_robin 使用）
        self._last_random_file = None  # 上次随机发送的文件名（避免连续重复）
        self._auto_sending = False  # 自动发送递归抑制标志
        self._suppress_until_at: dict = {}  # 会话 -> 硬抑制截止时间（monotonic）
        self._last_auto_sent_at: dict = {}  # 会话 -> 上次自动发送时间（monotonic）

        # 情感分析状态
        self._emotion_cache: dict = {}  # 会话 -> (monotonic 时间, 标签/None)
        self._emotion_lock = None  # 懒创建的 asyncio.Lock（防并发重复调用 LLM）

        # 标签数据（文件名 -> 标签列表），启动时加载
        self._tags: dict = {}
        try:
            self._tags = _load_tags_sync()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 加载表情标签失败: {e}")
            self._tags = {}

        # 自动创建表情存储目录（不存在则初始化）
        try:
            EMOTICONS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"[{PLUGIN_NAME}] 创建表情目录失败，请检查插件目录权限: {e}")

        # 初始化表情图床：markdown 图片必须走公网 URL，这里把表情目录对外提供
        if self.md_send_enabled:
            try:
                static_host.configure(
                    base_url=self.md_image_host_url,
                    bind=self.md_image_host_bind,
                    port=self.md_image_host_port,
                    root=EMOTICONS_DIR,
                )
                static_host.start()
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 表情图床初始化失败: {e}")
        else:
            logger.info(
                f"[{PLUGIN_NAME}] 未启用 markdown 图床"
                f"（md_send_enabled={self.md_send_enabled}, "
                f"md_image_host_url={'已填' if self.md_image_host_url else '空'}），"
                "表情将回落为普通图片消息"
            )

        # 注册 WebUI 管理接口（Plugin Pages 规范）
        self._register_web_apis()

        # ============================================================
        # 扩展预留区（后续可在此扩展新功能）
        # 1. 表情分组：例如 self.groups = {"可爱": ["a.jpg", ...]}，
        #    再增加 #表情分组 xxx 指令与 WebUI 分组管理接口。
        # 2. 关键词匹配：例如 self.keywords = {"开心": ["b.gif", ...]}，
        #    消息命中关键词时自动发送对应表情（与 tag 策略可联动）。
        # 3. 分组管理：在 WebUI 中为表情分组，指令 #表情分组 xxx 发送组内表情。
        # 4. 新增 Web 接口：在 _register_web_apis 中继续调用
        #    self.context.register_web_api(...) 即可。
        # ============================================================
        self.groups: dict = {}  # 分组名 -> 表情文件名列表
        self.keywords: dict = {}  # 关键词 -> 表情文件名列表

    # ---------------- Web 接口注册 ----------------
    def _register_web_apis(self) -> None:
        """注册插件 WebUI 管理接口（路由必须带插件名前缀）。

        页面（pages/manage）通过桥接层调用，实际对外路径为：
        /api/v1/plugins/extensions/emoticon_manager/<接口名>
        """
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/list",
            self.api_list,
            ["GET"],
            "获取全部表情包列表",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upload",
            self.api_upload,
            ["POST"],
            "上传表情包图片",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/delete",
            self.api_delete,
            ["DELETE", "POST"],
            "删除表情包",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/file",
            self.api_file,
            ["GET"],
            "获取表情包文件内容",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/tag",
            self.api_tag,
            ["POST"],
            "设置表情包标签",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/tags",
            self.api_tags,
            ["GET"],
            "获取全部表情标签及统计",
        )

    # ---------------- Web 接口：GET list ----------------
    async def api_list(self):
        """GET /emoticon_manager/list —— 获取全部表情包（含在线预览数据）。"""
        try:
            files = await asyncio.to_thread(self._list_with_preview)
        except OSError as e:
            logger.error(f"[{PLUGIN_NAME}] 读取表情目录失败: {e}")
            return error_response("读取表情目录失败", status_code=500)
        # 附带每个表情的情感标签
        for item in files:
            item["tags"] = list(self._tags.get(item["filename"], []))
        return json_response({"total": len(files), "files": files})

    # ---------------- Web 接口：POST upload ----------------
    async def api_upload(self):
        """POST /emoticon_manager/upload —— 上传表情包图片（表单字段 file）。

        兼容不同 AstrBot 版本的上传对象实现（鸭子类型判断），并支持
        根据文件头识别真实格式，自动修正缺失或不规范的扩展名。
        """
        try:
            EMOTICONS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 创建表情目录失败: {e}")
            return error_response(f"创建表情目录失败: {e}", status_code=500)

        # 1. 解析上传表单（兼容 dict / MultiDict 等不同返回结构）
        try:
            form_files = await request.files()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 解析上传表单失败: {e}")
            return error_response(f"解析上传表单失败: {e}", status_code=400)

        upload = None
        if isinstance(form_files, dict):
            upload = form_files.get("file")
        elif hasattr(form_files, "getlist"):
            items = form_files.getlist("file")
            upload = items[0] if items else None
        if upload is None:
            return error_response("缺少上传文件字段 file", status_code=400)
        # 鸭子类型判断：不同 AstrBot 版本的上传对象类名可能不同
        if not hasattr(upload, "filename") or not (
            hasattr(upload, "save") or hasattr(upload, "file")
        ):
            return error_response("上传文件对象不合法", status_code=400)

        original_name = str(getattr(upload, "filename", "") or "").strip()

        # 2. 上传前大小预检（部分版本的上传对象自带 size 字段）
        pre_size = getattr(upload, "size", None)
        try:
            if pre_size is not None and int(pre_size) > MAX_UPLOAD_BYTES:
                return error_response("文件超过 8MB 大小限制", status_code=413)
        except (TypeError, ValueError):
            pass

        # 3. 写入临时文件（兼容 async / sync 两种 save 实现）
        tmp_path = (
            EMOTICONS_DIR / f".upload_tmp_{int(time.time() * 1000)}_{os.getpid()}"
        )
        try:
            if hasattr(upload, "save"):
                result = upload.save(tmp_path)
                if asyncio.iscoroutine(result):
                    await result
            else:
                # 回退：从 file-like 对象流式写出
                src = getattr(upload, "file", upload)
                with tmp_path.open("wb") as f:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)

            # 4. 上传后大小校验（以实际落盘字节数为准）
            size = tmp_path.stat().st_size
            if size <= 0:
                return error_response("文件内容为空", status_code=400)
            if size > MAX_UPLOAD_BYTES:
                return error_response("文件超过 8MB 大小限制", status_code=413)

            # 5. 读取文件头，识别真实图片格式
            with tmp_path.open("rb") as f:
                head = f.read(16)

            # 5.1 ZIP 压缩包：安全解压并自动提取符合规范的图片
            if _is_zip_head(head):
                return await asyncio.to_thread(
                    self._extract_zip, tmp_path, original_name
                )

            detected_ext = _detect_image_type(head)

            # 6. 校验 / 修正文件名与扩展名
            safe_name = _safe_filename(original_name)
            if (
                safe_name is None
                or Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS
            ):
                # 文件名缺少扩展名或扩展名不在白名单：
                # 用文件头识别出的真实格式自动修正扩展名
                if detected_ext is None:
                    return error_response(
                        "文件不是有效的 jpg/jpeg/png/gif 图片（或扩展名与真实格式不符），"
                        "已拒绝上传",
                        status_code=400,
                    )
                base = re.sub(r"\.[^.]+$", "", original_name) or "表情包"
                safe_name = _safe_filename(f"{base}{detected_ext}")
                if safe_name is None:
                    return error_response("文件名不合法", status_code=400)
            else:
                # 扩展名在允许列表内：文件头必须与扩展名一致，防止改名伪装
                if not _check_image_magic(head, Path(safe_name).suffix.lower()):
                    return error_response(
                        "文件内容与图片格式不符（扩展名与真实格式不一致），已拒绝",
                        status_code=400,
                    )

            # 7. 生成不重复文件名并原子移入表情目录（防止覆盖已有表情）
            with _FILE_LOCK:
                final_name = self._unique_filename(safe_name)
                os.replace(tmp_path, EMOTICONS_DIR / final_name)

            logger.info(f"[{PLUGIN_NAME}] 上传新表情包: {final_name} ({size} bytes)")
            ext = Path(final_name).suffix.lower()
            return json_response(
                {
                    "ok": True,
                    "filename": final_name,
                    "size": size,
                    "content_type": CONTENT_TYPES.get(ext, "application/octet-stream"),
                }
            )
        except Exception as e:
            # 捕获一切异常并把具体原因回传页面，便于排查
            logger.exception(f"[{PLUGIN_NAME}] 保存上传文件失败: {e}")
            return error_response(f"上传失败: {e}", status_code=500)
        finally:
            # 清理残留的临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------- Web 接口：POST upload（ZIP 压缩包批量导入） ----------------
    def _extract_zip(self, zip_path: Path, original_name: str):
        """解压 ZIP 压缩包，自动提取其中符合规范的表情包图片。

        安全防护：
        1. 压缩包条目名一律经 _safe_filename 清洗（去路径成分、白名单后缀），
           再 resolve() 校验最终路径仍在表情目录内，拦截 zip-slip 路径穿越；
        2. 解压炸弹防护：单文件大小、解压总大小、条目数量三重上限，
           超限立即中止并返回 413；
        3. 每个条目解压后校验文件头魔数，拒绝改名伪装的非图片文件；
        4. 同名文件自动追加序号，不覆盖已有表情；
        5. 解压临时文件先写后原子替换，失败自动清理。

        返回结构：
        {
          "ok": true,
          "type": "zip",
          "filename": "表情包合集.zip",
          "extracted": ["a.jpg", "b.png"],   # 成功提取的文件名
          "skipped": ["readme.txt"],          # 被跳过的条目
          "extracted_count": 2,
          "skipped_count": 1
        }
        """
        extracted = []  # 成功提取的表情文件名
        skipped = []  # 被跳过的条目及原因

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
                # 条目数量上限：防止超大量条目拖垮服务器
                if len(infos) > MAX_ZIP_ENTRY_COUNT:
                    return error_response(
                        f"ZIP 内条目过多（>{MAX_ZIP_ENTRY_COUNT} 个），已拒绝",
                        status_code=413,
                    )
                # 解压总大小上限：防止“解压炸弹”
                total_size = sum(i.file_size for i in infos)
                if total_size > MAX_ZIP_TOTAL_BYTES:
                    return error_response(
                        "ZIP 解压后总大小超过 64MB 限制，已拒绝",
                        status_code=413,
                    )

                for info in infos:
                    raw_name = _decode_zip_name(info.filename)
                    # 目录条目跳过
                    if info.is_dir():
                        continue
                    # 条目名清洗（含路径穿越防护与后缀白名单）
                    safe_name = _safe_filename(raw_name)
                    if safe_name is None:
                        skipped.append(f"{raw_name}（非图片文件或文件名不合法）")
                        continue
                    # 单文件大小上限
                    if info.file_size > MAX_UPLOAD_BYTES:
                        skipped.append(f"{raw_name}（超过 8MB）")
                        continue
                    if info.file_size <= 0:
                        skipped.append(f"{raw_name}（空文件）")
                        continue

                    # 读取条目内容（文件头校验用）
                    try:
                        data = zf.read(info)
                    except (RuntimeError, zipfile.BadZipFile, OSError) as e:
                        # RuntimeError 常见于加密条目等无法读取的情况
                        logger.warning(
                            f"[{PLUGIN_NAME}] ZIP 条目读取失败 {raw_name}: {e}"
                        )
                        skipped.append(f"{raw_name}（读取失败）")
                        continue
                    if not data:
                        skipped.append(f"{raw_name}（内容为空）")
                        continue
                    # 校验文件头与扩展名一致，防止改名伪装
                    ext = Path(safe_name).suffix.lower()
                    if not _check_image_magic(data[:16], ext):
                        skipped.append(f"{raw_name}（内容与图片格式不符）")
                        continue

                    # 写入表情目录：同名自动加序号，原子替换
                    try:
                        with _FILE_LOCK:
                            final_name = self._unique_filename(safe_name)
                            dst = EMOTICONS_DIR / final_name
                            tmp_dst = EMOTICONS_DIR / (
                                f".zip_tmp_{int(time.time() * 1000)}_{os.getpid()}"
                                f"_{len(extracted)}"
                            )
                            try:
                                with tmp_dst.open("wb") as f:
                                    f.write(data)
                                os.replace(tmp_dst, dst)
                            finally:
                                try:
                                    tmp_dst.unlink(missing_ok=True)
                                except OSError:
                                    pass
                        extracted.append(final_name)
                    except OSError as e:
                        logger.error(
                            f"[{PLUGIN_NAME}] ZIP 条目写入失败 {raw_name}: {e}"
                        )
                        skipped.append(f"{raw_name}（写入失败）")

        except zipfile.BadZipFile as e:
            logger.error(f"[{PLUGIN_NAME}] 无效的 ZIP 文件: {e}")
            return error_response("不是有效的 ZIP 压缩包", status_code=400)
        except Exception as e:
            logger.exception(f"[{PLUGIN_NAME}] 解压 ZIP 失败: {e}")
            return error_response(f"解压 ZIP 失败: {e}", status_code=500)

        logger.info(
            f"[{PLUGIN_NAME}] ZIP 导入完成: {original_name} "
            f"成功 {len(extracted)} 个，跳过 {len(skipped)} 个"
        )
        return json_response(
            {
                "ok": True,
                "type": "zip",
                "filename": original_name,
                "extracted": extracted,
                "skipped": skipped,
                "extracted_count": len(extracted),
                "skipped_count": len(skipped),
            }
        )

    # ---------------- Web 接口：DELETE / POST delete ----------------
    async def api_delete(self):
        """DELETE /emoticon_manager/delete?filename=xxx —— 删除表情包。

        同时兼容 POST：页面桥接层没有 apiDelete 方法，
        页面通过 bridge.apiPost("delete", {filename: ...}) 调用。
        """
        if request.method == "DELETE":
            filename = request.query.get("filename")
        else:
            payload = await request.json(default={})
            filename = (
                (payload or {}).get("filename") if isinstance(payload, dict) else None
            )

        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        target = _resolve_emoticon(filename)
        if target is None:
            return error_response("非法文件名", status_code=400)

        with _FILE_LOCK:
            if not target.is_file():
                return error_response(f"未找到表情文件: {filename}", status_code=404)
            try:
                target.unlink()
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 删除表情包失败 {filename}: {e}")
                return error_response(f"删除失败: {e}", status_code=500)

        # 同步清理该文件的标签数据（失败不影响删除结果，仅记录日志）
        with _TAG_LOCK:
            if self._tags.pop(target.name, None) is not None:
                try:
                    _save_tags_sync(self._tags)
                except OSError as e:
                    logger.error(f"[{PLUGIN_NAME}] 清理标签数据失败: {e}")

        logger.info(f"[{PLUGIN_NAME}] 已删除表情包: {target.name}")
        return json_response({"ok": True, "filename": target.name})

    # ---------------- Web 接口：GET file ----------------
    async def api_file(self):
        """GET /emoticon_manager/file?filename=xxx —— 获取表情文件内容。

        供页面下载大文件使用（大文件不内嵌 base64 预览）。
        """
        filename = request.query.get("filename")
        target = _resolve_emoticon(filename)
        if target is None:
            return error_response("非法文件名", status_code=400)
        if not target.is_file():
            return error_response("文件不存在", status_code=404)
        ext = Path(target.name).suffix.lower()
        return file_response(
            target,
            filename=target.name,
            content_type=CONTENT_TYPES.get(ext, "application/octet-stream"),
        )

    # ---------------- Web 接口：POST tag ----------------
    async def api_tag(self):
        """POST /emoticon_manager/tag —— 设置表情包的标签。

        JSON 请求体支持三种动作：
        - set（默认）：{"filename": "a.jpg", "tags": ["开心", "搞笑"]}
          整体替换该文件的标签列表；
        - add：{"filename": "a.jpg", "action": "add", "tag": "开心"}
          追加单个标签（已存在则忽略）；
        - remove：{"filename": "a.jpg", "action": "remove", "tag": "开心"}
          移除单个标签。

        文件名仍走 _resolve_emoticon 路径穿越防护，且必须真实存在。
        """
        try:
            payload = await request.json(default={})
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 解析标签请求失败: {e}")
            return error_response(f"解析请求失败: {e}", status_code=400)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        filename = str(payload.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)
        target = _resolve_emoticon(filename)
        if target is None:
            return error_response("非法文件名", status_code=400)
        if not target.is_file():
            return error_response(f"未找到表情文件: {filename}", status_code=404)

        action = str(payload.get("action") or "set").strip().lower()
        if action not in ("set", "add", "remove"):
            return error_response("action 仅支持 set / add / remove", status_code=400)

        with _TAG_LOCK:
            current = list(self._tags.get(target.name, []))
            if action in ("add", "remove"):
                tag = _sanitize_tag(payload.get("tag"))
                if tag is None:
                    return error_response(
                        "标签不合法（空/过长/含非法字符）", status_code=400
                    )
                if action == "add":
                    if tag not in current:
                        current.append(tag)
                else:
                    current = [t for t in current if t != tag]
            else:
                raw_list = payload.get("tags")
                if not isinstance(raw_list, list):
                    return error_response("set 动作需要 tags 数组", status_code=400)
                current = []
                for t in raw_list:
                    t = _sanitize_tag(t)
                    if t and t not in current:
                        current.append(t)
                    if len(current) >= MAX_TAGS_PER_FILE:
                        break

            if current:
                self._tags[target.name] = current
            else:
                self._tags.pop(target.name, None)
            try:
                _save_tags_sync(self._tags)
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 保存标签数据失败: {e}")
                return error_response(f"保存标签失败: {e}", status_code=500)

        logger.info(
            f"[{PLUGIN_NAME}] 设置标签 {target.name}: "
            f"{current if current else '(无标签)'}"
        )
        return json_response({"ok": True, "filename": target.name, "tags": current})

    # ---------------- Web 接口：GET tags ----------------
    async def api_tags(self):
        """GET /emoticon_manager/tags —— 获取全部标签及统计（供页面筛选栏）。"""
        counter: dict = {}
        try:
            files = await asyncio.to_thread(self._list_emoticons)
            valid_names = {f["filename"] for f in files}
        except OSError:
            valid_names = set()

        with _TAG_LOCK:
            tags_snapshot = {
                k: list(v) for k, v in self._tags.items() if k in valid_names
            }
        for fname, tag_list in tags_snapshot.items():
            for t in tag_list:
                counter.setdefault(t, []).append(fname)

        items = [{"tag": t, "count": len(fs), "files": fs} for t, fs in counter.items()]
        items.sort(key=lambda x: (-x["count"], x["tag"]))
        return json_response({"tags": items, "total": len(items)})

    # ---------------- markdown 图片发送：辅助方法 ----------------
    def _md_available(self) -> bool:
        """当前是否具备 markdown 图片发送条件（开关 + 图床就绪）。"""
        return bool(self.md_send_enabled and static_host.is_enabled())

    def _build_markdown_for(self, picked: list, with_prefix: bool = True) -> str:
        """把选中的表情拼成 markdown 图片语法；失败返回空串。

        每条形如 ``![名字 #宽px #高px](http://.../emoticons/xxx.png)``，
        宽高由「显示上限 × 缩放系数」算出（只改显示尺寸，不动原文件）。

        Args:
            picked: 选中的表情文件信息列表。
            with_prefix: 是否在图片前加一段颜文字；单独补发时为 True，
                拼进已有回复时为 False。
        """
        blocks = []
        for f in picked:
            name = str(f.get("filename") or "")
            url = static_host.url_for(name)
            if not url:
                continue
            w, h = static_host.image_size(EMOTICONS_DIR / name)
            dw, dh = static_host.md_display_size(
                w, h, self.md_max_width, self.md_max_height, self.md_scale
            )
            if dw <= 0 or dh <= 0:
                # 尺寸解析失败时给个兜底方框，避免 QQ 只显示 [名字] 方块
                dw, dh = self.md_max_width, self.md_max_height
            alt = Path(name).stem or "表情"
            blocks.append(static_host.image_markdown(url, alt, dw, dh))
        if not blocks:
            return ""
        body = "\n".join(blocks)
        if not with_prefix:
            return body
        return f"{MD_PREFIX_TEXT}\n{body}"

    async def _should_auto_send(self, event) -> bool:
        """自动发送的概率 + 冷却判定；命中则记录本次发送时间。"""
        if not self.auto_send_enabled or self.emoticon_probability <= 0:
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        now = time.monotonic()
        if now < self._suppress_until_at.get(umo, 0.0):
            return False
        if random.random() * 100 >= self.emoticon_probability:
            return False
        if now - self._last_auto_sent_at.get(umo, 0.0) < self.min_interval_seconds:
            return False
        self._last_auto_sent_at[umo] = now
        if len(self._last_auto_sent_at) > 2000:
            self._last_auto_sent_at.clear()
            self._suppress_until_at.clear()
        return True

    # ---------------- 发送前：把表情拼进同一条回复消息 ----------------
    @filter.on_agent_done()
    async def _mark_turn_done(self, event: AstrMessageEvent, run_context, response):
        """Agent 运行完成时打标记，供发送后钩子判断“整次回复已结束”。"""
        event.set_extra("_emoticon_turn_done", True)

    @filter.on_decorating_result()
    async def attach_emoticon_to_reply(self, event: AstrMessageEvent):
        """发送前钩子：把随机表情以 markdown 图片拼接到本条回复后面（同一气泡）。

        仅在「markdown 总开关 + 附着开关 + 图床就绪」时生效；命中后打标记，
        让 after_message_sent 不再单独补发一条，避免重复。
        """
        if not event.get_extra("_emoticon_turn_done"):
            return
        if not (self._md_available() and self.md_attach_to_reply):
            return
        if self._auto_sending:
            return
        if event.get_extra("_emoticon_md_attached") or event.get_extra(
            "_emoticon_manual"
        ):
            return

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if not await self._should_auto_send(event):
            return

        files = await asyncio.to_thread(self._list_emoticons)
        if not files:
            return
        picked = await self._pick_emoticons(event, files, self.max_emoticons_per_reply)
        if not picked:
            return
        md = self._build_markdown_for(picked, with_prefix=False)
        if not md:
            return

        merged = False
        for comp in reversed(result.chain):
            if isinstance(comp, Plain):
                comp.text = f"{comp.text}\n\n{md}"
                merged = True
                break
        if not merged:
            result.chain.append(Plain(f"\n\n{md}"))
        try:
            result.use_markdown(True)
        except Exception:
            pass
        event.set_extra("_emoticon_md_attached", True)
        logger.info(
            f"[{PLUGIN_NAME}] 已在回复中附带 markdown 表情: "
            f"{[f['filename'] for f in picked]}"
        )

    # ---------------- 自动发送：机器人回复后随机补发表情 ----------------
    @filter.after_message_sent()
    async def auto_send_emoticon(self, event: AstrMessageEvent):
        """机器人每发送一条消息后自动触发：按概率随机补发一张表情包。

        说明：
        - 事件钩子（after_message_sent）不能用 yield 发消息，必须用 event.send()。
        - 若表情已在发送前拼进同一条回复（attach_emoticon_to_reply），
          本方法会跳过，避免重复发送。
        - 触发条件：自动发送开关开启、表情库非空、概率命中、且同一会话
          距离上次自动发送超过最小间隔（防止一条对话连续刷出多张表情）。
        - 递归防护：自动发送的表情本身也会触发本钩子，通过
          _auto_sending 标志 + 硬抑制窗口 + 会话冷却三层防止死循环。
        """
        # 只有整次回复结束时才补发（中途的工具前提示等不触发）
        if not event.get_extra("_emoticon_turn_done"):
            return
        # 已拼进同一条回复 → 不重复补发
        if event.get_extra("_emoticon_md_attached"):
            return
        # 手动指令（#表情包）本次已发送表情 → 不再自动补发
        if event.get_extra("_emoticon_manual"):
            return
        if self._auto_sending:
            return
        if not self.auto_send_enabled or self.emoticon_probability <= 0:
            return

        files = await asyncio.to_thread(self._list_emoticons)
        if not files:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        if not await self._should_auto_send(event):
            return

        picked = await self._pick_emoticons(event, files, self.max_emoticons_per_reply)
        if not picked:
            return
        paths = [EMOTICONS_DIR / f["filename"] for f in picked]

        try:
            self._auto_sending = True
            self._suppress_until_at[umo] = time.monotonic() + 2.0
            # markdown 可用时优先走 markdown（图 + 文一条消息）
            if self._md_available():
                md = self._build_markdown_for(picked)
                if md:
                    res = event.make_result()
                    res.message(md)
                    res.use_markdown(True)
                    await event.send(res)
                    logger.info(
                        f"[{PLUGIN_NAME}] 自动发送 markdown 表情: "
                        f"{[p.name for p in paths]} (概率 {self.emoticon_probability}%)"
                    )
                    return
            if len(paths) == 1:
                # 单张：直接发送图片消息
                await event.send(event.image_result(str(paths[0])))
            else:
                # 多张：组装成一条图片消息链发送
                chain = [Image.fromFileSystem(str(p)) for p in paths]
                await event.send(event.chain_result(chain))
            logger.info(
                f"[{PLUGIN_NAME}] 自动发送表情包: "
                f"{[p.name for p in paths]} (概率 {self.emoticon_probability}%)"
            )
        except Exception as e:
            logger.exception(f"[{PLUGIN_NAME}] 自动发送表情包失败: {e}")
        finally:
            # 保持一小段抑制窗口，防止刚发出的表情再次触发本钩子
            await asyncio.sleep(0.5)
            self._auto_sending = False

    # ---------------- 指令：#表情包 / #随机表情 ----------------
    @filter.command("表情包")
    @filter.command("随机表情")
    async def random_emoticon(self, event: AstrMessageEvent):
        """#表情包 / #随机表情 —— 手动发送表情包（不受概率限制，始终发送）"""
        # 标记本次为手动发送，避免发送前钩子再自动附加一张
        event.set_extra("_emoticon_manual", True)
        files = await asyncio.to_thread(self._list_emoticons)
        if not files:
            yield event.plain_result("暂无表情包，请前往插件管理页面上传！")
            return

        # 手动指令不参与概率判定，直接按选择策略发送
        picked = await self._pick_emoticons(event, files, self.max_emoticons_per_reply)
        if not picked:
            yield event.plain_result(
                "当前为情感匹配策略，但没有匹配到合适标签的表情包，"
                "请到 WebUI 管理页面为表情打标签，或更换选择策略。"
            )
            return
        paths = [EMOTICONS_DIR / f["filename"] for f in picked]

        # markdown 可用时优先走 markdown（图 + 尺寸控制）
        if self._md_available():
            md = self._build_markdown_for(picked)
            if md:
                res = event.make_result()
                res.message(md)
                res.use_markdown(True)
                yield res
                return

        if len(paths) == 1:
            # 单张：直接发送图片消息
            yield event.image_result(str(paths[0]))
            return
        # 多张：组装成一条图片消息链发送，算作“一次回复”
        chain = [Image.fromFileSystem(str(p)) for p in paths]
        yield event.chain_result(chain)

    # ---------------- 指令：#表情设置 ----------------
    @filter.command("表情设置")
    async def show_emoticon_settings(self, event: AstrMessageEvent):
        """#表情设置 —— 查看当前表情配置（自动发送 / 选择策略）"""
        if self.selection_strategy == "round_robin":
            strategy_desc = "轮流发送（round_robin，同一轮内不重复）"
        elif self.selection_strategy == "tag":
            strategy_desc = "情感匹配（tag，总结对话判断情感标签后挑选表情）"
        else:
            strategy_desc = "随机发送（random，避免连续两次重复）"
        auto_desc = "开启" if self.auto_send_enabled else "关闭"
        emotion_desc = "开启" if self.emotion_match_enabled else "关闭"
        emotion_tags = self._collect_emotion_tags()
        yield event.plain_result(
            "==== 🎯 表情包配置 ====\n"
            f"自动发送（回复后随机补发）：{auto_desc}\n"
            f"选择策略：{strategy_desc}\n"
            f"情感匹配：{emotion_desc}\n"
            f"情感标签（自动获取 {len(emotion_tags)} 个）："
            f"{' / '.join(emotion_tags) or '（表情库暂无标签，请到管理页面为表情打标签）'}\n"
            f"每次回复最多使用表情：{self.max_emoticons_per_reply} 个\n"
            f"表情出现概率：{self.emoticon_probability}%\n"
            f"同一会话最小间隔：{self.min_interval_seconds} 秒\n"
            "手动指令 #表情包 / #随机表情 不受概率限制，始终发送\n"
            "情感匹配策略需配合 tag 策略与 WebUI 标签一起使用\n"
            "修改方式：AstrBot WebUI → 插件 → 情绪表情包 → 设置"
        )

    # ---------------- 指令：#表情列表 ----------------
    @filter.command("表情列表")
    async def list_emoticons(self, event: AstrMessageEvent):
        """#表情列表 [页码] —— 分页输出所有表情包文件名"""
        files = await asyncio.to_thread(self._list_emoticons)
        if not files:
            yield event.plain_result("暂无表情包，请前往插件管理页面上传！")
            return

        args = self._extract_args(event.message_str, "表情列表")
        try:
            page = int(args[0]) if args else 1
        except (TypeError, ValueError):
            page = 1
        page = max(1, page)

        total = len(files)
        max_page = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, max_page)

        start = (page - 1) * PAGE_SIZE
        chunk = files[start : start + PAGE_SIZE]
        lines = [f"{start + i + 1}. {f['filename']}" for i, f in enumerate(chunk)]
        if page < max_page:
            footer = (
                f"—— 第 {page}/{max_page} 页，发送 #表情列表 {page + 1} 查看下一页 ——"
            )
        else:
            footer = f"—— 第 {page}/{max_page} 页 ——"
        yield event.plain_result(
            "\n".join([f"📚 共 {total} 个表情包："] + lines + [footer])
        )

    # ---------------- 指令：#表情删除 ----------------
    @filter.command("表情删除")
    async def delete_emoticon(self, event: AstrMessageEvent):
        """#表情删除 文件名 —— 删除指定表情包"""
        args = self._extract_args(event.message_str, "表情删除")
        if not args:
            yield event.plain_result(
                "用法：#表情删除 文件名\n"
                "例如：#表情删除 猫猫.jpg\n"
                "可先发送 #表情列表 查看所有表情包文件名。"
            )
            return

        filename = args[0]
        target = _resolve_emoticon(filename)
        if target is None:
            # 忽略大小写再尝试一次，提升易用性
            target = self._find_case_insensitive(filename)
        if target is None:
            yield event.plain_result(
                f"未找到名为「{filename}」的表情包文件，请发送 #表情列表 确认文件名。"
            )
            return

        with _FILE_LOCK:
            try:
                target.unlink()
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 删除表情包失败 {filename}: {e}")
                yield event.plain_result(f"删除失败：{e}")
                return
        # 同步清理该文件的标签数据
        with _TAG_LOCK:
            if self._tags.pop(target.name, None) is not None:
                try:
                    _save_tags_sync(self._tags)
                except OSError as e:
                    logger.error(f"[{PLUGIN_NAME}] 清理标签数据失败: {e}")
        yield event.plain_result(f"✅ 已删除表情包：{target.name}")

    # ---------------- 内部工具方法 ----------------
    @staticmethod
    def _extract_args(message_str: str, command: str) -> list:
        """从原始消息中提取指令参数：去掉唤醒前缀与指令名后按空白拆分。"""
        text = re.sub(r"^[/#!．。]+\s*", "", str(message_str or "").strip())
        if text.startswith(command):
            text = text[len(command) :].strip()
        return [p for p in re.split(r"[,，\s]+", text) if p]

    @staticmethod
    def _list_emoticons() -> list:
        """同步扫描表情目录（指令与接口共用的数据源）。"""
        return _scan_emoticons_sync()

    def _list_with_preview(self) -> list:
        """扫描表情目录并为小文件生成 base64 预览数据。"""
        files = _scan_emoticons_sync()
        for item in files:
            if item["size"] > PREVIEW_MAX_BYTES:
                continue
            try:
                with (EMOTICONS_DIR / item["filename"]).open("rb") as f:
                    data = f.read(PREVIEW_MAX_BYTES + 1)
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 读取预览失败 {item['filename']}: {e}")
                continue
            if not data or len(data) > PREVIEW_MAX_BYTES:
                continue
            b64 = base64.b64encode(data).decode("ascii")
            item["preview"] = f"data:{item['content_type']};base64,{b64}"
            item["previewable"] = True
        return files

    @staticmethod
    def _clamp_int(value, default, low, high) -> int:
        """把配置值安全转换为 [low, high] 区间内的整数，异常时回退默认值。"""
        try:
            num = int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            num = default
        return max(low, min(high, num))

    async def _pick_emoticons(self, event, files: list, count: int) -> list:
        """按表情选择策略挑选 count 个互不重复的表情文件（async 版本）。

        - random：洗牌后随机取前 count 个，并保证与上次发送的首图不重复；
        - round_robin：按文件名顺序轮流取，游标持续推进，
          表情文件增删后仍能自动归一，不会连续两轮重复；
        - tag：只总结机器人本次输出的内容（不附加对话上下文），
          判断情感标签后优先挑选带该标签的表情包；情感匹配未开启
          或匹配失败时按配置回退随机 / 不发送。
        """
        if not files:
            return []
        count = max(1, min(int(count), len(files)))

        if self.selection_strategy == "tag":
            picked = await self._pick_by_emotion(event, files, count)
            if picked is not None:
                return picked
            # picked 为 None 表示情感匹配不可用/失败，按配置回退
            if not self.emotion_fallback_random:
                logger.info(
                    f"[{PLUGIN_NAME}] 情感匹配未命中且未开启回退随机，本次不发送"
                )
                return []
            logger.info(f"[{PLUGIN_NAME}] 情感匹配失败/未命中，回退随机发送")

        return self._pick_sync(files, count)

    def _pick_sync(self, files: list, count: int) -> list:
        """random / round_robin 的同步挑选逻辑（tag 回退时复用）。"""
        if not files:
            return []
        count = max(1, min(int(count), len(files)))

        if self.selection_strategy == "round_robin":
            total = len(files)
            picked = []
            picked_names = set()
            idx = self._rr_index
            # 最多完整遍历一轮，保证挑选 count 个互不重复的文件
            for _ in range(total):
                item = files[idx % total]
                if item["filename"] not in picked_names:
                    picked.append(item)
                    picked_names.add(item["filename"])
                idx += 1
                if len(picked) >= count:
                    break
            self._rr_index = (self._rr_index + count) % total
            return picked

        return self._pick_from_candidates(files, count)

    def _pick_from_candidates(self, candidates: list, count: int) -> list:
        """在候选列表内随机挑选 count 个，尽量不与上次发送的首图重复。"""
        candidates = list(candidates)
        random.shuffle(candidates)
        if self._last_random_file and len(candidates) > 1:
            # 若洗牌后第一张与上次发送重复，则与第二张交换，避免连续重复
            if candidates[0]["filename"] == self._last_random_file:
                candidates[0], candidates[1] = candidates[1], candidates[0]
        picked = candidates[:count]
        if picked:
            self._last_random_file = picked[-1]["filename"]
        return picked

    async def _pick_by_emotion(self, event, files: list, count: int) -> list | None:
        """情感匹配策略：分析机器人本次输出 -> 按情感标签挑选表情。

        返回：
        - list：成功按标签选出表情（数量不足时用随机表情补齐）；
        - None：情感匹配未开启、LLM 不可用或未匹配到任何标签表情。
        """
        if not self.emotion_match_enabled:
            logger.info(f"[{PLUGIN_NAME}] 选择策略为 tag 但情感匹配未开启，回退随机")
            return None

        tag = await self._analyze_emotion_tag(event)
        if not tag:
            return None

        tag_low = tag.lower()
        # 注意：运行时文件列表来自 _scan_emoticons_sync()，其中不包含
        # tags 字段（只有 WebUI 的 api_list 会额外补上）。因此这里必须
        # 直接查内存中的标签数据 self._tags（key 为文件名），
        # 否则永远匹配不到任何表情。
        matched = [
            f
            for f in files
            if any(
                str(t).strip().lower() == tag_low
                for t in self._tags.get(f["filename"], [])
            )
        ]
        if not matched:
            logger.info(f"[{PLUGIN_NAME}] 未找到带「{tag}」标签的表情包，回退")
            return None

        picked = self._pick_from_candidates(matched, count)
        if len(picked) < count:
            # 标签表情不够时，用随机表情补齐到 count 个
            picked_names = {p["filename"] for p in picked}
            rest = [f for f in files if f["filename"] not in picked_names]
            random.shuffle(rest)
            picked.extend(rest[: count - len(picked)])
        logger.info(
            f"[{PLUGIN_NAME}] 情感标签「{tag}」匹配表情: "
            f"{[p['filename'] for p in picked]}"
        )
        return picked

    async def _analyze_emotion_tag(self, event) -> str | None:
        """分析机器人本次输出的内容，返回匹配的情感标签（无则 None）。

        说明：
        - 只读取事件中机器人刚发送的回复文本（after_message_sent 时
          event.get_result() 仍持有本次发送结果），不附加对话上下文；
        - 结果按会话缓存 EMOTION_CACHE_TTL 秒，降低 LLM 调用频率；
        - 任何异常都会安全降级返回 None（由调用方决定回退随机）。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        now = time.monotonic()
        cached = self._emotion_cache.get(umo)
        if cached and now - cached[0] < EMOTION_CACHE_TTL:
            return cached[1]

        # 懒创建锁（asyncio.Lock 首次使用时才绑定事件循环）
        if self._emotion_lock is None:
            self._emotion_lock = asyncio.Lock()
        async with self._emotion_lock:
            # 二次检查缓存（等待锁期间可能有其他请求已完成分析）
            cached = self._emotion_cache.get(umo)
            if cached and now - cached[0] < EMOTION_CACHE_TTL:
                return cached[1]

            tag = None
            try:
                reply_text = self._get_reply_text(event)
                if not reply_text:
                    logger.info(f"[{PLUGIN_NAME}] 本次输出无可分析文本，跳过情感分析")
                    return None
                provider = self._get_emotion_provider(umo)
                if provider is None:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 未找到可用的对话模型，情感匹配不可用"
                    )
                    return None
                tag = await self._call_emotion_llm(provider, reply_text)
            except Exception as e:
                logger.exception(f"[{PLUGIN_NAME}] 情感分析失败: {e}")
            finally:
                # 缓存 None 同样有价值：避免短时间内反复失败重试
                self._emotion_cache[umo] = (time.monotonic(), tag)
                if len(self._emotion_cache) > 2000:
                    self._emotion_cache.clear()
            return tag

    @staticmethod
    def _get_reply_text(event) -> str:
        """读取机器人本次发送的回复纯文本（after_message_sent 事件）。"""
        try:
            result = event.get_result()
        except Exception:
            return ""
        if result is None:
            return ""
        get_plain = getattr(result, "get_plain_text", None)
        if callable(get_plain):
            try:
                return str(get_plain() or "").strip()
            except Exception:
                return ""
        return str(result or "").strip()

    def _get_emotion_provider(self, umo: str):
        """获取用于情感分析的对话模型（当前会话使用的 Provider）。"""
        try:
            provider = self.context.get_using_provider(umo)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 获取对话模型失败: {e}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        return provider

    def _collect_emotion_tags(self) -> list:
        """自动收集表情库中全部表情的实际标签（去重、排序）。

        标签数据来自 data/tags.json（WebUI 打标签时写入），
        因此情感匹配的候选标签永远与表情库保持一致：
        未在表情上出现过的标签不会出现在 LLM 的选项中。
        表情库没有任何标签时返回空列表。
        """
        seen: list[str] = []
        with _TAG_LOCK:
            for tag_list in self._tags.values():
                if not isinstance(tag_list, list):
                    continue
                for t in tag_list:
                    t = _sanitize_tag(t)
                    if t and t not in seen:
                        seen.append(t)
        return sorted(seen)

    async def _call_emotion_llm(self, provider, reply_text: str) -> str | None:
        """调用 LLM 总结机器人输出并返回匹配的情感标签（不匹配返回 None）。"""
        tags = self._collect_emotion_tags()
        if not tags:
            logger.info(f"[{PLUGIN_NAME}] 表情库暂无标签，跳过情感分析")
            return None
        tags_text = "、".join(tags)
        system_prompt = (
            "你是一个表情包情感分析助手。下面是机器人刚刚输出的回复内容，"
            "请只根据这段内容本身判断它的情绪氛围，不要联想、不要补充任何"
            "上下文。然后只从下面的标签列表中选择最合适的一个标签作为输出，"
            "不要输出任何解释、标点或多余文字：\n"
            f"可选标签：{tags_text}"
        )
        prompt = f"机器人输出内容：\n---\n{reply_text}\n---\n只输出一个标签。"
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, system_prompt=system_prompt),
            timeout=EMOTION_LLM_TIMEOUT,
        )
        text = ""
        if resp is not None:
            text = getattr(resp, "completion_text", None) or ""
            if not text and getattr(resp, "result_chain", None):
                try:
                    text = resp.result_chain.get_plain_text()
                except Exception:
                    text = ""
        return self._match_emotion_tag(str(text), tags)

    def _match_emotion_tag(self, text: str, tags: list | None = None) -> str | None:
        """从 LLM 输出中解析出与候选标签列表匹配的情感标签。"""
        if not text:
            return None
        if tags is None:
            tags = self._collect_emotion_tags()
        low_text = text.lower()
        # 先尝试整段文本是否直接包含某个标签
        for t in tags:
            if t and t.lower() in low_text:
                return t
        # 再按分隔符拆出候选词精确匹配
        candidates = [
            c.strip().strip("，。,.、!！?？;；:：\"'“”‘’[]【】()（）")
            for c in re.split(r"[,，、\s]+", text)
            if c.strip()
        ]
        for c in candidates:
            for t in tags:
                if t and c.lower() == t.lower():
                    return t
        return None

    @staticmethod
    def _unique_filename(safe_name: str) -> str:
        """同名文件存在时自动追加 _1/_2... 序号，避免覆盖已有表情。"""
        target = EMOTICONS_DIR / safe_name
        if not target.exists():
            return safe_name
        stem, ext = Path(safe_name).stem, Path(safe_name).suffix
        for i in range(1, 1000):
            candidate = f"{stem}_{i}{ext}"
            if not (EMOTICONS_DIR / candidate).exists():
                return candidate
        # 极端情况兜底：追加时间戳
        return f"{stem}_{int(time.time())}{ext}"

    def _find_case_insensitive(self, filename: str) -> Path:
        """忽略大小写查找表情文件（供 #表情删除 使用），找不到返回 None。"""
        low = str(filename).lower()
        for item in self._list_emoticons():
            if item["filename"].lower() == low:
                return EMOTICONS_DIR / item["filename"]
        return None

    # ---------------- 生命周期 ----------------
    async def terminate(self):
        """插件被停用/卸载时调用：停止本地图床静态服务。"""
        try:
            static_host.stop()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 停止表情图床失败: {e}")
