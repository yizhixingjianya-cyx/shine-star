"""复现并验证「主动回复时应该 @ 触发者，但没有 @」的修复。

`result_decorate/stage.py` 原先让 @ 与引用回复共用同一个门控：

    can_decorate = all(isinstance(item, (Plain, Image)) for item in result.chain)
    if can_decorate:
        if self.reply_with_mention and ...:      # @ 在这里面
        if self.reply_with_quote:                # 引用也在这里面

只要消息链里出现 Plain/Image 之外的组件，整段装饰（含 @）就被整体跳过，
且没有任何日志。最容易踩到的情形：

  * 开了 TTS：本 stage 前面的 TTS 分支把 Plain 换成 Record；
  * 回复内容本身带 At / Reply / Node（转发、引用、富文本）。

表现为「应该 @ 触发者，但就是没有 @」。

注意：本测试只分析 ResultDecorateStage.process() 内部，
      因为 initialize() 里也有一次 self.reply_with_mention 赋值。
"""

import inspect
import re

from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.pipeline.result_decorate import stage as stage_module

_CLASS_SRC = inspect.getsource(stage_module.ResultDecorateStage)


def _method_src(name: str) -> str:
    """取出类里某个方法的源码。"""
    match = re.search(
        rf"\n    (?:async )?def {name}\(.*?(?=\n    (?:async )?def |\Z)",
        _CLASS_SRC,
        re.S,
    )
    assert match, f"未找到方法 {name}"
    return match.group(0)


PROCESS_SRC = _method_src("process")


# --------------------------------------------------------------- 结构断言


def test_at_logic_present_in_process():
    """前提：process() 里确实有 @ 逻辑。"""
    assert "self.reply_with_mention" in PROCESS_SRC
    assert "can_decorate = all(" in PROCESS_SRC


def test_can_decorate_computed_before_inserting_at():
    """can_decorate 必须在插入 At 之前求值。

    否则刚插入的 At 会让 can_decorate 恒为 False，引用回复被连带关掉。
    """
    decorate_pos = PROCESS_SRC.find("can_decorate = all(")
    mention_pos = PROCESS_SRC.find("self.reply_with_mention")

    assert decorate_pos < mention_pos, (
        "can_decorate 在 @ 之后才求值：插入的 At 会让它恒为 False，引用回复将永久失效"
    )


def test_mention_not_nested_inside_can_decorate_block():
    """@ 语句不应位于 `if can_decorate:` 的缩进块内。"""
    lines = PROCESS_SRC.splitlines()

    gate_line = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("if can_decorate")),
        None,
    )
    assert gate_line is not None, "未找到 if can_decorate"

    gate_indent = len(lines[gate_line]) - len(lines[gate_line].lstrip())

    # 收集门控行之后、缩进仍深于门控的行（即门控块内部）
    inside_block: list[str] = []
    for line in lines[gate_line + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= gate_indent:
            break
        inside_block.append(line)

    nested = [ln for ln in inside_block if "self.reply_with_mention" in ln]
    assert not nested, (
        f"@ 逻辑仍在 if can_decorate 块内，Record/Reply/Node 会把 @ 一起挡掉：{nested}"
    )


def test_mention_condition_does_not_reference_can_decorate():
    """@ 的插入条件本身不依赖 can_decorate。"""
    idx = PROCESS_SRC.find("self.reply_with_mention")
    # 往前取到最近的 if 语句起点，截出条件表达式
    window = PROCESS_SRC[max(0, idx - 300) : idx]
    if_start = window.rfind("if ")
    assert if_start != -1, "未找到包裹 @ 的 if"

    condition = window[if_start:] + PROCESS_SRC[idx : idx + 120]
    condition = condition[: condition.find("):") + 2]

    assert "can_decorate" not in condition, f"@ 条件仍依赖 can_decorate：{condition!r}"


def test_quote_still_gated():
    """引用回复应保留原门控（平台普遍不支持对语音/转发做引用）。"""
    assert "can_decorate and self.reply_with_quote" in PROCESS_SRC, (
        "引用回复的门控被改动了"
    )


# --------------------------------------------------- 谓词语义（未改动）


def test_gating_predicate_semantics_unchanged():
    """can_decorate 自身的语义未被改动（Plain/Image 才算可装饰）。"""

    def can_decorate(chain):
        return all(isinstance(item, (Plain, Image)) for item in chain)

    assert can_decorate([Plain(text="hi")])
    assert can_decorate([Image.fromURL("http://x/y.png")])
    assert not can_decorate([Record(file="v.silk")])
    assert not can_decorate([Reply(id="m")])
    assert not can_decorate([At(qq="1")])


def test_initialize_still_reads_config():
    """配置读取仍在，且 initialize 里不应出现装饰逻辑。"""
    init_src = _method_src("initialize")
    assert "reply_with_mention" in init_src
    assert "result.chain.insert" not in init_src
