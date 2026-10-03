"""广播插件测试夹具：平台实例、客户端与事件替身"""

import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_DIR.parent

for path in (PLUGINS_DIR, PLUGIN_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

GROUP_OPENID = "DC1EA5204DB05C55AF8C01988BC2047B"
GROUP_NUM = "123456"
USER_OPENID = "EB4E842FE856B5073A152CC9277FD1B6"
PLATFORM_ID = "default_102092713"


class FakeOfficialClient:
    """QQ 官方机器人客户端替身"""

    def __init__(self, group_name: str = "1"):
        self.group_name = group_name
        self.posts: list[dict] = []
        self.http_calls: list[tuple[str, str]] = []
        self.api = SimpleNamespace(
            post_group_message=self._post_group_message,
            _http=SimpleNamespace(request=self._http_request),
        )

    async def _post_group_message(self, **kwargs):
        self.posts.append(kwargs)
        return SimpleNamespace(id="MSG")

    async def _http_request(self, route, **kwargs):
        self.http_calls.append((route.method, route.path))
        return {"group_openid": GROUP_OPENID, "group_name": self.group_name}


class FakeOneBotClient:
    """OneBot 客户端替身"""

    def __init__(self, responses: dict | None = None, fail_actions: set | None = None):
        self.responses = responses or {}
        self.fail_actions = set(fail_actions or ())
        self.actions: list[tuple[str, dict]] = []
        self.api = SimpleNamespace(call_action=self._call_action)

    async def _call_action(self, action: str, **kwargs):
        self.actions.append((action, kwargs))
        if action in self.fail_actions:
            raise RuntimeError(f"OneBot 接口 {action} 调用失败")
        return self.responses.get(action, {})

    def called(self, action: str) -> list[dict]:
        return [kwargs for name, kwargs in self.actions if name == action]


class FakePlatform:
    """平台适配器实例替身"""

    def __init__(self, name: str, platform_id: str, client):
        self._name = name
        self._id = platform_id
        self._client = client

    def meta(self):
        return SimpleNamespace(name=self._name, id=self._id)

    def get_client(self):
        return self._client


class FakeContext:
    """Star Context 替身"""

    def __init__(self, platforms: list):
        self.platform_manager = SimpleNamespace(platform_insts=platforms)
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, umo, message_chain) -> bool:
        text = "".join(
            seg.text for seg in getattr(message_chain, "chain", []) if hasattr(seg, "text")
        )
        self.sent.append((str(umo), text))
        return True


class FakeEvent:
    """群聊消息事件替身"""

    def __init__(self, platform_id: str, group_id: str, message_str: str = ""):
        self._platform_id = platform_id
        self._group_id = group_id
        self.message_str = message_str

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_group_id(self) -> str:
        return self._group_id

    def plain_result(self, text: str):
        return SimpleNamespace(text=text)


async def collect(generator) -> list[str]:
    return [result.text async for result in generator]
