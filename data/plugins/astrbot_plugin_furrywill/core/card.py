"""每日鉴毛卡片排版（由 cyxbot pyfurbot/command/furry_will.py 移植）。

保留原有 markdown 排版规则：名字一级标题 + 加粗、期号一级引用、三项信息二级引用、
出品方斜体署名；普通消息回落用纯文本版。

markdown 成品::

    # **篝橙**

    > 【第1644期】
    >> 地区：包头
    >> 种族：龙
    >> 工作室：可乐酱

    _狼神米迩_
"""

from __future__ import annotations

import re

__all__ = [
    "build_card_text",
    "build_card_markdown",
    "build_card_keyboard",
    "parse_args",
    "CARD_MD_SCALE",
]

# 卡片图回填尺寸 = 官方建议上限 × 该比例（只改显示尺寸，图床上仍是原图）
CARD_MD_SCALE = 0.75

CARD_NAME = "# **{name}**"
CARD_QISHU = "> 【第{qishu}期】"
CARD_QUOTE = ">> "
CARD_FOOTER = "{producer}"

CARD_FIELDS = (
    ("地区", "city"),
    ("种族", "race"),
    ("工作室", "studio"),
)


def _card_name(card: dict) -> str:
    """名字（一级标题 + 加粗），没名字就返回空串。"""
    name = (card.get("name") or "").strip()
    return CARD_NAME.format(name=name) if name else ""


def _card_footer(producer: str = "") -> str:
    """底部署名 = 出品方名字；没给出品方就返回空串。"""
    return CARD_FOOTER.format(producer=(producer or "").strip())


def _card_fields(card: dict) -> list:
    """要展示的字段，返回 ``[(标签, 值)]``，空值整条略过。"""
    return [(label, card.get(key)) for label, key in CARD_FIELDS if card.get(key)]


def _card_quote(card: dict) -> list:
    """引用区块里的行：期号一级引用，三项信息二级引用。"""
    lines = []
    qishu = card.get("qishu")
    if qishu:
        lines.append(CARD_QISHU.format(qishu=qishu))
    lines += [f"{CARD_QUOTE}{label}：{value}" for label, value in _card_fields(card)]
    return lines


def _card_blocks(card: dict, producer: str = "") -> list:
    """卡片正文主体的「块」：名字 / 引用区 / 署名。"""
    blocks = [b for b in (_card_name(card),) if b]
    quoted = _card_quote(card)
    if quoted:
        blocks.append("\n".join(quoted))
    footer = _card_footer(producer)
    if footer:
        blocks.append(f"_{footer}_")
    return blocks


def build_card_text(card: dict, producer: str = "") -> str:
    """纯文本版卡片正文（非 markdown 平台回落时发）。"""
    lines = []
    name = (card.get("name") or "").strip()
    if name:
        lines.append(name)
    qishu = card.get("qishu")
    if qishu:
        lines.append(f"【第{qishu}期】")
    for label, value in _card_fields(card):
        lines.append(f"{label}：{value}")
    footer = _card_footer(producer)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def build_card_markdown(card: dict, producer: str = "") -> str:
    """markdown 版卡片正文（图片由调用方拼在最前面）。"""
    return "\n\n".join(_card_blocks(card, producer))


# ── 卡片下方的按钮（keyboard）─────────────────────────────────────────
# QQ 官方 markdown 消息可以挂 keyboard（按钮），和正文同一条消息发出，不用多发一条。
# 这里用**指令按钮**（action.type=2 + enter=False）：点一下只把指令填进输入框、
# 不替用户发送 —— 「再来一个」想直接抽就直接发，想改参数（期数 / 名字 / 城市）
# 就在输入框里接着补，改完再发。仅 QQ Official 群聊 / 单聊支持，其它平台会忽略。
CARD_BUTTON_LABEL = "再来一个"  # 按钮上的字
CARD_BUTTON_CMD = "/每日鉴毛"  # 点一下填进输入框的指令（带唤醒前缀）


def _card_button() -> dict:
    """卡片下方的按钮：点击把 ``CARD_BUTTON_CMD`` 填进输入框，不自动发送。"""
    return {
        "id": "furrywill:again",  # 按钮标识，同一条消息里唯一即可
        "render_data": {
            "label": CARD_BUTTON_LABEL,
            "visited_label": CARD_BUTTON_LABEL,
            "style": 1,
        },
        "action": {
            "type": 2,  # 指令按钮：自动在输入框插入 @机器人 + data
            "permission": {"type": 2},  # 所有人可操作
            "data": CARD_BUTTON_CMD,
            "enter": False,  # 只填入输入框，不自动发送
            "reply": False,  # 不引用原消息
            "unsupport_tips": "当前QQ版本不支持按钮，请升级后重试",
        },
    }


def build_card_keyboard() -> dict:
    """卡片下方的键盘（一行一个按钮），随 markdown 消息一起下发。"""
    return {"content": {"rows": [{"buttons": [_card_button()]}]}}


def parse_args(raw: str) -> tuple[str | None, str]:
    """把指令参数解析为 ``(action, keyword)``；无法识别返回 ``(None, "")``。

    action ∈ {``random``, ``qishu``, ``name``, ``city``, ``studio``}。
    """
    if not raw:
        return "random", ""
    if raw.isdigit():
        return "qishu", raw

    m = re.match(r"^(?:第)?\s*(\d+)\s*期$", raw)
    if m:
        return "qishu", m.group(1)

    m = re.match(r"^(城市|地区)\s*[:：]?\s*(.+?)\s*$", raw)
    if m:
        return "city", m.group(2).strip()

    m = re.match(r"^工作室\s*[:：]?\s*(.+?)\s*$", raw)
    if m:
        return "studio", m.group(1).strip()

    return "name", raw


def help_text() -> str:
    """指令用法帮助文案。"""
    return (
        "每日鉴毛 用法：\n"
        "  每日鉴毛            随机抽卡\n"
        "  每日鉴毛 600        查询第600期\n"
        "  每日鉴毛 佩里斯      按名称搜索\n"
        "  每日鉴毛 城市 北京   按地区搜索\n"
        "  每日鉴毛 工作室 XX   按工作室搜索"
    )
