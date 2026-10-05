from collections.abc import AsyncGenerator
from contextlib import aclosing

from astrbot.core import logger
from astrbot.core.config.agent_runner import normalize_agent_runner
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.session_llm_manager import SessionServiceManager

from ...context import PipelineContext
from ..stage import Stage
from .agent_sub_stages.internal import InternalAgentSubStage
from .agent_sub_stages.third_party import ThirdPartyAgentSubStage


class AgentRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.ctx = ctx
        self.config = ctx.astrbot_config

        self.bot_wake_prefixs: list[str] = self.config["wake_prefix"]
        self.prov_wake_prefix: str = self.config["provider_settings"]["wake_prefix"]
        for bwp in self.bot_wake_prefixs:
            if self.prov_wake_prefix.startswith(bwp):
                logger.info(
                    f"The additional LLM wake prefix {self.prov_wake_prefix} starts "
                    f"with the bot wake prefix {bwp}; the duplicate prefix was "
                    "removed automatically.",
                )
                self.prov_wake_prefix = self.prov_wake_prefix[len(bwp) :]

        agent_runner = normalize_agent_runner(self.config.get("agent_runner"))
        self.config["agent_runner"] = agent_runner
        agent_runner_type = agent_runner["runner_type"]
        if agent_runner_type == "local":
            self.agent_sub_stage = InternalAgentSubStage()
        else:
            self.agent_sub_stage = ThirdPartyAgentSubStage()
        await self.agent_sub_stage.initialize(ctx)

    async def process(self, event: AstrMessageEvent) -> AsyncGenerator[None, None]:
        if not self.ctx.astrbot_config["provider_settings"]["enable"]:
            logger.debug(
                "This pipeline does not enable AI capability, skip processing."
            )
            return

        if not await SessionServiceManager.should_process_llm_request(event):
            logger.debug(
                f"The session {event.unified_msg_origin} has disabled AI capability, skipping processing."
            )
            return

        # 用 aclosing 显式关闭子生成器, 保证管道提前停止时该生成器被同步关闭,
        # 其内部持有的 session lock 等资源得以释放.
        async with aclosing(
            self.agent_sub_stage.process(event, self.prov_wake_prefix)
        ) as agent_agen:
            async for resp in agent_agen:
                yield resp
