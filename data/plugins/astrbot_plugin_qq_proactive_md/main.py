"""QQ 官方机器人独有能力的 AstrBot LLM 工具封装。

把 QQ 官方机器人（``qq_official`` / ``qq_official_webhook``）独有的
**原生 Markdown、消息按钮（keyboard）、Ark 卡片、主动推送** 封装成大模型可调用的
FunctionTool，使 AstrBot 的 Agent 能像调用普通工具一样直接下发这些消息类型。

核心发送逻辑在 ``qq_md_core.py``（不依赖 AstrBot，可独立测试）。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .qq_md_core import (
    QQ_PLATFORM_NAMES,
    SCENE_AUTO,
    SCENE_C2C,
    SCENE_CHANNEL,
    SCENE_DM,
    SCENE_GROUP,
    SCENES,
    QQNativeError,
    QQNativeSender,
    normalize_target_id,
    scene_label,
    split_session,
)

_MAX_BUTTON_ROWS = 5
_MAX_BUTTONS_PER_ROW = 5

_SCENE_PROPERTY = {
    "type": "string",
    "enum": list(SCENES),
    "description": (
        "Target scene, which selects the QQ send API. "
        "auto detects it from the session; group=QQ group chat, c2c=QQ direct chat, "
        "channel=guild text channel, dm=guild direct message."
    ),
}

_SESSION_PROPERTY = {
    "type": "string",
    "description": (
        "Target session in the form platform_id:message_type:session_id. "
        "Leave empty for the current session. Targeting another session requires admin permission."
    ),
}

_PROACTIVE_PROPERTY = {
    "type": "boolean",
    "description": (
        "Whether to push as a proactive message. true pushes without referencing any message "
        "(required for cron jobs or unprompted outreach); false replies to the current message. "
        "Defaults to false."
    ),
}

_MSG_ID_PROPERTY = {
    "type": "string",
    "description": (
        "Message ID to reply to. Usually leave empty: the current message is quoted automatically."
    ),
}


def _scene_from_event(event: Any) -> str | None:
    """根据当前事件携带的 QQ 原始消息推断场景。

    Args:
        event: AstrBot 消息事件。

    Returns:
        场景标识；无法判断时返回 ``None``。
    """
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw_message is None:
        return None
    if hasattr(raw_message, "group_openid"):
        return SCENE_GROUP
    if hasattr(raw_message, "channel_id"):
        return SCENE_CHANNEL
    if hasattr(raw_message, "guild_id"):
        return SCENE_DM
    if hasattr(raw_message, "author"):
        return SCENE_C2C
    return None


async def _resolve_sender(
    context: ContextWrapper[AstrAgentContext],
    *,
    session: str | None,
    scene: str | None,
    markdown_fallback_to_plain: bool,
) -> tuple[QQNativeSender, str, str] | str:
    """解析目标会话并构造 QQ 原生消息发送器。

    Args:
        context: 当前 Agent 执行上下文。
        session: 目标会话，留空表示当前会话。
        scene: 目标场景，``auto`` 表示自动判断。
        markdown_fallback_to_plain: 是否在平台拒绝原生 Markdown 时降级为纯文本。

    Returns:
        ``(sender, scene, target_id)`` 三元组；解析失败时返回以 ``error:`` 开头的字符串。
    """
    event = context.context.event
    current_session = event.unified_msg_origin
    target_session = (session or "").strip() or current_session

    if target_session != current_session:
        is_admin = event.is_admin() if hasattr(event, "is_admin") else False
        if not is_admin:
            return (
                "error: Only admins can send messages to other sessions. "
                "Ask the user to add their ID to the admins list in "
                "AstrBot WebUI -> Config -> General Config."
            )

    try:
        platform_id, message_type, session_id = split_session(target_session)
    except QQNativeError as exc:
        return f"error: {exc}"

    platform = context.context.context.get_platform_inst(platform_id)
    if platform is None:
        return f"error: Platform instance '{platform_id}' was not found or is disabled."
    if platform.meta().name not in QQ_PLATFORM_NAMES:
        return (
            f"error: Current platform is '{platform.meta().name}'. These tools only work on "
            "QQ Official Bot platforms (qq_official / qq_official_webhook); "
            "use `send_message_to_user` instead."
        )

    api = getattr(platform.get_client(), "api", None)
    if api is None:
        return "error: The QQ bot client is not ready yet, please retry later."

    resolved_scene = (scene or SCENE_AUTO).strip().lower()
    if resolved_scene == SCENE_AUTO:
        resolved_scene = (
            _scene_from_event(event) if target_session == current_session else None
        ) or (SCENE_GROUP if message_type == "GroupMessage" else SCENE_C2C)

    target_id = normalize_target_id(session_id, message_type)
    if not target_id:
        return "error: Target session ID is empty, nothing to send to."
    return (
        QQNativeSender(api, markdown_fallback_to_plain=markdown_fallback_to_plain),
        resolved_scene,
        target_id,
    )


def _resolve_reply_msg_id(
    context: ContextWrapper[AstrAgentContext],
    *,
    proactive: bool,
    msg_id: str | None,
) -> str | None:
    """决定被动回复使用的 ``msg_id``。

    Args:
        context: 当前 Agent 执行上下文。
        proactive: 是否强制走主动推送。
        msg_id: 调用方显式指定的消息 ID。

    Returns:
        需要引用的消息 ID；主动推送或没有可引用消息时返回 ``None``。
    """
    if proactive:
        return None
    if msg_id:
        return str(msg_id)
    return getattr(context.context.event.message_obj, "message_id", None) or None


@dataclass
class QQSendMarkdownTool(FunctionTool[AstrAgentContext]):
    """发送 QQ 原生 Markdown 消息（可挂载按钮）。"""

    name: str = "qq_send_markdown"
    description: str = (
        "Send a native QQ Official Bot Markdown message, optionally with inline buttons. "
        "ONLY available on QQ Official Bot platforms (qq_official / qq_official_webhook); "
        "on any other platform use `send_message_to_user` instead. "
        "Prefer this tool when the reply benefits from QQ native Markdown "
        "(headings, bold, links, images, lists, quotes) or when the user should tap buttons. "
        "It supports passive replies (quoting the current message) and proactive pushes "
        "(e.g. cron jobs); plain text replies can still be answered directly."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Custom Markdown text, e.g. '# Daily\\n**Key**：[site](https://example.com)'. "
                        "Provide either content or template_id."
                    ),
                },
                "template_id": {
                    "type": "string",
                    "description": (
                        "Markdown template ID issued by the QQ Open Platform. "
                        "Provide either content or template_id."
                    ),
                },
                "template_params": {
                    "type": "object",
                    "description": (
                        'Template variables, e.g. {"title": "Hello"}. '
                        'Use an array when one key needs several values: {"tags": ["a", "b"]}.'
                    ),
                },
                "keyboard_id": {
                    "type": "string",
                    "description": (
                        "Button template ID issued by the QQ Open Platform. "
                        "Mutually exclusive with buttons."
                    ),
                },
                "buttons": {
                    "type": "array",
                    "description": (
                        f"Custom inline buttons (requires QQ allow-listing). At most "
                        f"{_MAX_BUTTON_ROWS} rows with {_MAX_BUTTONS_PER_ROW} buttons each. "
                        'Example: {"label": "Check in", "data": "checkin", "type": 2, "row": 0}.'
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Button text."},
                            "data": {
                                "type": "string",
                                "description": (
                                    "Button payload: a URL when type=0, otherwise sent back to the bot."
                                ),
                            },
                            "type": {
                                "type": "integer",
                                "description": "0 link, 1 callback, 2 command. Defaults to 2.",
                            },
                            "row": {
                                "type": "integer",
                                "description": "Row index starting from 0. Defaults to 0.",
                            },
                            "visited_label": {
                                "type": "string",
                                "description": "Button text after being clicked.",
                            },
                            "style": {
                                "type": "integer",
                                "description": "0 grey outline, 1 blue outline. Defaults to 0.",
                            },
                            "permission": {
                                "type": "integer",
                                "description": (
                                    "0 specific users, 1 admins only, 2 everyone, "
                                    "3 specific roles (guild only). Defaults to 2."
                                ),
                            },
                        },
                        "required": ["label"],
                    },
                },
                "session": _SESSION_PROPERTY,
                "scene": _SCENE_PROPERTY,
                "proactive": _PROACTIVE_PROPERTY,
                "msg_id": _MSG_ID_PROPERTY,
                "plain_fallback": {
                    "type": "string",
                    "description": (
                        "Plain text used when the bot lacks the native Markdown permission. "
                        "Leave empty to derive it from content automatically."
                    ),
                },
            },
        }
    )

    markdown_fallback_to_plain: bool = True

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """发送 QQ 原生 Markdown 消息。

        Args:
            context: 当前 Agent 执行上下文。
            **kwargs: 见 ``parameters`` 定义。

        Returns:
            发送结果描述，失败时以 ``error:`` 开头。
        """
        content = str(kwargs.get("content") or "").strip()
        template_id = str(kwargs.get("template_id") or "").strip()
        if not content and not template_id:
            return "error: Provide either content (custom Markdown) or template_id."

        resolved = await _resolve_sender(
            context,
            session=kwargs.get("session"),
            scene=kwargs.get("scene"),
            markdown_fallback_to_plain=self.markdown_fallback_to_plain,
        )
        if isinstance(resolved, str):
            return resolved
        sender, scene, target_id = resolved

        try:
            return await sender.send_markdown(
                scene=scene,
                target_id=target_id,
                content=content or None,
                template_id=template_id or None,
                params=kwargs.get("template_params") or None,
                keyboard_id=str(kwargs.get("keyboard_id") or "").strip() or None,
                buttons=kwargs.get("buttons") or None,
                msg_id=_resolve_reply_msg_id(
                    context,
                    proactive=bool(kwargs.get("proactive")),
                    msg_id=kwargs.get("msg_id"),
                ),
                plain_fallback=str(kwargs.get("plain_fallback") or "").strip() or None,
            )
        except QQNativeError as exc:
            return f"error: Failed to send QQ native Markdown: {exc}"


@dataclass
class QQSendArkTool(FunctionTool[AstrAgentContext]):
    """发送 QQ Ark 模版卡片消息。"""

    name: str = "qq_send_ark"
    description: str = (
        "Send a QQ Official Bot Ark template card (rich link card, music card, etc.). "
        "ONLY available on QQ Official Bot platforms. "
        "Ark template IDs are issued by the QQ Open Platform, so never invent one: "
        "ask the user for the template ID when it is unknown."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "template_id": {
                    "type": "integer",
                    "description": "Ark template ID issued by the QQ Open Platform.",
                },
                "kv": {
                    "type": "array",
                    "description": "Ark template variables.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Variable name."},
                            "value": {
                                "type": "string",
                                "description": "Variable value.",
                            },
                        },
                        "required": ["key", "value"],
                    },
                },
                "session": _SESSION_PROPERTY,
                "scene": _SCENE_PROPERTY,
                "proactive": _PROACTIVE_PROPERTY,
                "msg_id": _MSG_ID_PROPERTY,
            },
            "required": ["template_id"],
        }
    )

    markdown_fallback_to_plain: bool = True

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """发送 QQ Ark 模版卡片。

        Args:
            context: 当前 Agent 执行上下文。
            **kwargs: 见 ``parameters`` 定义。

        Returns:
            发送结果描述，失败时以 ``error:`` 开头。
        """
        if kwargs.get("template_id") in (None, ""):
            return "error: template_id (an Ark template ID from the QQ Open Platform) is required."

        resolved = await _resolve_sender(
            context,
            session=kwargs.get("session"),
            scene=kwargs.get("scene"),
            markdown_fallback_to_plain=self.markdown_fallback_to_plain,
        )
        if isinstance(resolved, str):
            return resolved
        sender, scene, target_id = resolved

        try:
            return await sender.send_ark(
                scene=scene,
                target_id=target_id,
                template_id=kwargs.get("template_id"),
                kv=kwargs.get("kv") or None,
                msg_id=_resolve_reply_msg_id(
                    context,
                    proactive=bool(kwargs.get("proactive")),
                    msg_id=kwargs.get("msg_id"),
                ),
            )
        except QQNativeError as exc:
            return f"error: Failed to send QQ Ark card: {exc}"


@dataclass
class QQSessionInfoTool(FunctionTool[AstrAgentContext]):
    """查询当前 QQ 会话信息，便于选择合适的发送方式与目标。"""

    name: str = "qq_get_session_info"
    description: str = (
        "Inspect the current QQ Official Bot session: platform, scene (group/c2c/channel/dm), "
        "target ID, and whether proactive push is supported. "
        "Call this before `qq_send_markdown` or `qq_send_ark` when unsure how to address the "
        "current QQ session."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """返回当前 QQ 会话的关键信息。

        Args:
            context: 当前 Agent 执行上下文。
            **kwargs: 未使用。

        Returns:
            会话信息文本，或错误描述。
        """
        del kwargs
        event = context.context.event
        current_session = event.unified_msg_origin
        try:
            platform_id, message_type, session_id = split_session(current_session)
        except QQNativeError as exc:
            return f"error: {exc}"

        platform = context.context.context.get_platform_inst(platform_id)
        if platform is None:
            return f"error: Platform instance '{platform_id}' was not found."
        if platform.meta().name not in QQ_PLATFORM_NAMES:
            return (
                f"session: {current_session}\n"
                f"platform: {platform.meta().name} "
                "(not a QQ Official Bot platform, these tools are unavailable)"
            )

        scene = _scene_from_event(event) or (
            SCENE_GROUP if message_type == "GroupMessage" else SCENE_C2C
        )
        lines = [
            f"session: {current_session}",
            f"platform: {platform.meta().name} ({platform.meta().id})",
            f"scene: {scene_label(scene)}",
            f"target_id: {normalize_target_id(session_id, message_type)}",
            f"message_type: {message_type}",
            f"proactive_push_supported: {'yes' if platform.meta().support_proactive_message else 'no'}",
            f"current_message_id: {getattr(event.message_obj, 'message_id', None) or 'none'}",
        ]
        return "\n".join(lines)


def build_tools(settings: dict[str, Any] | None = None) -> list[FunctionTool]:
    """根据插件配置构建全部工具实例。

    Args:
        settings: 插件持久化配置，支持 ``markdown_fallback_to_plain``。

    Returns:
        已实例化的 FunctionTool 列表。
    """
    markdown_fallback = bool((settings or {}).get("markdown_fallback_to_plain", True))
    return [
        QQSendMarkdownTool(markdown_fallback_to_plain=markdown_fallback),
        QQSendArkTool(markdown_fallback_to_plain=markdown_fallback),
        QQSessionInfoTool(),
    ]


@register(
    "astrbot_plugin_qq_proactive_md",
    "Trae",
    "把 QQ 官方机器人独有的原生 Markdown、消息按钮、Ark 卡片与主动推送封装成大模型可调用的工具。",
    "1.0.0",
)
class QQProactiveMdPlugin(Star):
    """注册 QQ 官方机器人独有能力对应的 LLM 工具。

    仅在 QQ 官方机器人平台（``qq_official`` / ``qq_official_webhook``）上生效，
    其他平台调用时会收到明确的错误提示。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context.add_llm_tools(*build_tools(dict(config or {})))
        logger.info(
            "[qq_proactive_md] Registered tools: qq_send_markdown, qq_send_ark, qq_get_session_info",
        )
