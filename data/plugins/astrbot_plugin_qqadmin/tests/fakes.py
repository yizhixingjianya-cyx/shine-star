"""测试夹具：官方机器人 / OneBot 客户端替身与事件构造"""

from pathlib import Path
from types import SimpleNamespace

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)

GROUP_ID = "30584554AA2BF4E72BD3B8F27A70339D"
SENDER_ID = "FE003FAF76C4817251FDC128A16753BB"
BOT_ID = "EC58D87F598C8294A533B9D458DAAF33"
TARGET_ID = "7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D"


class FakeHttp:
    """记录官方 API 请求的假 HTTP 客户端"""

    def __init__(
        self,
        responses: dict | None = None,
        error: Exception | None = None,
        errors: list | None = None,
    ):
        self.responses = responses or {}
        self.error = error
        self.errors = list(errors or [])
        self.calls: list[SimpleNamespace] = []

    async def request(self, route, **kwargs):
        self.calls.append(
            SimpleNamespace(
                method=route.method,
                path=route.path,
                url=route.url,
                json=kwargs.get("json"),
            )
        )
        if self.errors:
            raise self.errors.pop(0)
        if self.error is not None:
            raise self.error
        for keyword, response in self.responses.items():
            if keyword in route.path:
                return response
        return {}


class OfficialBot:
    """botpy Client 的最小替身"""

    def __init__(
        self,
        responses: dict | None = None,
        error: Exception | None = None,
        errors: list | None = None,
    ) -> None:
        self.http = FakeHttp(responses, error, errors)
        self.posts: list[dict] = []
        self.api = SimpleNamespace(
            _http=self.http, post_group_message=self._post_group_message
        )

    async def _post_group_message(self, **kwargs):
        self.posts.append(kwargs)
        return SimpleNamespace(id="MSG_OPENID")

    @property
    def calls(self) -> list[SimpleNamespace]:
        return self.http.calls


class OneBotClient:
    """OneBot 客户端替身，记录接口名与调用参数"""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.api = SimpleNamespace(call_action=self._call_action)

    async def _call_action(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        return self.responses.get(action, {"messages": []})

    def __getattr__(self, name: str):
        async def call(**kwargs):
            self.calls.append((name, kwargs))
            return self.responses.get(name, {})

        return call

    def called(self, name: str) -> list[dict]:
        return [kwargs for action, kwargs in self.calls if action == name]


class FakeDB:
    """QQAdminDB 替身

    真实实现会把默认群模板的字段合并进来，这里用 defaults 模拟取值兜底。
    """

    DEFAULTS = {
        "join_switch": False,
        "join_max_time": 3,
        "join_no_match_reject": False,
        "join_accept_words": [],
        "join_reject_words": [],
        "reject_word_block": False,
        "block_ids": [],
    }

    def __init__(
        self,
        values: dict | None = None,
        group_config: dict | None = None,
        group_ids: list | None = None,
    ) -> None:
        self.values = values or {}
        self.group_config = group_config or {}
        self.group_ids = group_ids if group_ids is not None else [GROUP_ID]
        self.nicknames: dict[str, dict[str, str]] = {}
        self.nickname_writes: list[tuple[str, dict]] = []

    def list_group_ids(self) -> list[str]:
        return list(self.group_ids)

    async def save_nicknames(self, group_id: str, names: dict) -> None:
        store = self.nicknames.setdefault(group_id, {})
        store.update({str(uid): str(name) for uid, name in names.items() if name})
        self.nickname_writes.append((group_id, dict(names)))

    async def load_nicknames(self) -> dict:
        return {gid: dict(names) for gid, names in self.nicknames.items()}

    def get_group_snapshot(self, group_id: str) -> dict:
        return dict(self.group_config)

    async def get(self, group_id: str, key: str, default=None):
        if key in self.values:
            return self.values[key]
        if key in self.DEFAULTS:
            return self.DEFAULTS[key]
        return default

    async def set(self, group_id: str, key: str, value) -> None:
        self.values[key] = value

    async def add(self, group_id: str, key: str, value) -> None:
        self.values.setdefault(key, []).append(value)


class FakeConfig:
    """PluginConfig 替身"""

    def __init__(
        self,
        ban_time: int = 60,
        perms: dict | None = None,
        admins_id: list | None = None,
        level_threshold: int = 50,
        admin_audit: bool = False,
    ) -> None:
        self.ban_time = ban_time
        self.perms = perms or {}
        self.admins_id = admins_id or []
        self.level_threshold = level_threshold
        self.admin_audit = admin_audit
        self.spamming_count = 5
        self.spamming_interval = 0.5
        self.ban_lexicon_path = Path(__file__).resolve().parent.parent / (
            "SensitiveLexicon.json"
        )

    def get_ban_time_with_range(self, random_ban_time=None, seconds=None) -> int:
        return self.ban_time if seconds is None else seconds


class FakePlatformInst:
    """平台适配器实例替身"""

    def __init__(self, name: str, client, config: dict | None = None):
        self._name = name
        self._client = client
        self.config = config or {}

    def meta(self):
        return SimpleNamespace(name=self._name)

    def get_client(self):
        return self._client


class FakePlugin:
    """插件替身，仅提供后台任务需要的 context"""

    def __init__(self, insts: list | None = None) -> None:
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=insts or [])
        )


class RawMessage:
    """官方机器人消息/事件对象（保留事件名与原始事件体）"""

    def __init__(self, data: dict, event_name: str = ""):
        self.raw_data = data
        self.event_name = event_name


class FakeEvent(AstrMessageEvent):
    """可直接用于群管逻辑的群聊事件"""

    def __init__(
        self,
        platform: str = "qq_official",
        group_id: str = GROUP_ID,
        sender_id: str = SENDER_ID,
        self_id: str = BOT_ID,
        message_str: str = "",
        message: list | None = None,
        message_id: str = "10086",
        raw_data: dict | None = None,
        event_name: str = "",
        raw_message=None,
        bot=None,
    ):
        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE
        abm.group_id = group_id
        abm.self_id = self_id
        abm.sender = MessageMember(user_id=sender_id, nickname="群友")
        abm.message_id = message_id
        abm.message = message or []
        abm.message_str = message_str
        if raw_message is not None:
            abm.raw_message = raw_message
        elif raw_data is not None:
            abm.raw_message = RawMessage(raw_data, event_name)
        else:
            abm.raw_message = None
        meta = PlatformMetadata(name=platform, description="test", id=f"{platform}-1")
        super().__init__(message_str, abm, meta, group_id)
        self.bot = bot
        self.sent: list = []

    async def send(self, message) -> None:
        self.sent.append(message)

    @property
    def sent_texts(self) -> list[str]:
        return [
            seg.text
            for result in self.sent
            for seg in (result.chain or [])
            if isinstance(seg, Plain)
        ]
