# QQ 原生 MD 工具（astrbot_plugin_qq_proactive_md）

把**只有 QQ 官方机器人（`qq_official` / `qq_official_webhook`）才有**的能力封装成 AstrBot 的
LLM 工具调用：原生 Markdown、消息按钮（keyboard）、Ark 卡片、以及主动推送。

AstrBot 自带的 `send_message_to_user` 只能发纯文本与富媒体，无法指定 `msg_type=2` 的原生
Markdown，也不能挂按钮。这个插件补齐了这部分。

## 注册的工具

| 工具 | 作用 |
| --- | --- |
| `qq_send_markdown` | 发送 QQ 原生 Markdown（`msg_type=2`），可选按钮模版或自定义按钮；支持被动回复与主动推送 |
| `qq_send_ark` | 发送 QQ Ark 模版卡片（`msg_type=3`） |
| `qq_get_session_info` | 查询当前 QQ 会话的场景（群聊 / 单聊 / 子频道 / 频道私信）、目标 ID、是否支持主动推送 |

### `qq_send_markdown` 主要参数

| 参数 | 说明 |
| --- | --- |
| `content` | 自定义 Markdown 文本，支持标题、加粗、斜体、链接、图片、列表、引用、分割线 |
| `template_id` / `template_params` | 使用 QQ 开放平台申请的 Markdown 模版（`custom_template_id` + `params`） |
| `keyboard_id` | 按钮模版 ID（与 `buttons` 二选一） |
| `buttons` | 自定义按钮，最多 5 行 × 每行 5 个；每项 `{label, data, type, row, style, permission}` |
| `session` | 目标会话 `platform_id:message_type:session_id`，留空表示当前会话 |
| `scene` | `auto` / `group` / `c2c` / `channel` / `dm`，决定调用哪个 QQ 发送接口 |
| `proactive` | `true` 走主动推送（不带 `msg_id`）；定时任务、主动搭话必须为 `true` |
| `plain_fallback` | 机器人没有原生 Markdown 权限时的降级纯文本；留空则根据 `content` 自动转换 |

## 四个 QQ 发送场景

| scene | QQ 接口 | 目标 ID |
| --- | --- | --- |
| `group` | `POST /v2/groups/{group_openid}/messages` | `group_openid` |
| `c2c` | `POST /v2/users/{openid}/messages` | 用户 `openid` |
| `channel` | `POST /channels/{channel_id}/messages` | `channel_id` |
| `dm` | `POST /dms/{guild_id}/messages` | `guild_id` |

`scene=auto`（默认）时，当前会话会依据 QQ 原始消息类型自动判断；跨会话发送则依据
`message_type` 推断（`GroupMessage` → `group`，`FriendMessage` → `c2c`）。

## 内置的健壮性处理

1. **被动转主动**：回复消息超过 5 分钟有效期或 `msg_id` 失效时，自动去掉 `msg_id` 改用主动推送重试一次。
2. **Markdown 降级**：平台返回「不允许发送原生 markdown」时，自动把 Markdown 转成纯文本，用 `msg_type=0` 重发（可在插件配置中关闭）。
3. **`msg_seq` 自动补全**：QQ 要求相同 `msg_id + msg_seq` 不能重复发送，主动推送时自动生成随机序号。
4. **权限收敛**：只有管理员才能把消息发到当前会话之外的会话，与 AstrBot 内置 `send_message_to_user` 的策略一致。

## 安装与使用

1. 把插件目录放到 AstrBot 的 `data/plugins/` 下（或在 WebUI 插件管理里上传本压缩包），重载插件。
2. 无需额外依赖：`qq-botpy` 已随 AstrBot 主程序安装。
3. 之后在与 QQ 官方机器人对话时，大模型即可自动调用 `qq_send_markdown` 等工具。

## 本地联调

```bash
python tests/run_tests.py
```

共 55 项断言，覆盖：工具注册与 JSON Schema、场景自动识别、目标会话解析、
跨会话权限、Markdown / 按钮 / Ark 的最终请求载荷、Markdown 降级、被动转主动重试。

由于本机没有 QQ 机器人的 `appid` / `secret` 与可用网关，测试用假的
`platform.get_client().api` 承接请求，断言到「最终 HTTP 请求体」这一层。
接入真实机器人后，可直接在与机器人的对话中调用工具完成端到端验证。
