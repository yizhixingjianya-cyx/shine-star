![AstrBot-Logo-Simplified](https://github.com/user-attachments/assets/36fb04e4-cc75-4454-bd8b-049d11aa86f9)

<div align="center">

<img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="python">
<img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-76bad9.svg" alt="license">

<br />

<a href="#快速开始">快速开始</a> ｜ <a href="#内置插件">内置插件</a> ｜ <a href="#部署">部署</a> ｜ <a href="https://docs.astrbot.app/">上游文档</a> ｜ <a href="https://github.com/fishpond-studio/shine-stars/issues">问题反馈</a>

</div>

> **说明**：本仓库 `shine-stars` 是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的修改版分支，遵循 **GNU AGPL-3.0-or-later** 协议分发。修改声明见 [NOTICE](NOTICE)，对应源码见 <https://github.com/fishpond-studio/shine-stars>。

## 简介

AstrBot 是一个开源的一站式 Agent 聊天机器人平台，可接入主流即时通讯应用，为个人、开发者和团队提供可靠、可扩展的对话式 AI 基础设施。

`shine-stars` 在上游 AstrBot 的基础上，预置了一组面向 QQ 社群（毛装 / 兽档案等）的常用插件，并修复了若干本地化使用问题，开箱即可用于社群机器人场景。除内置插件外，其余使用方式与上游保持一致，完整的使用与开发文档请参考 [官方文档](https://docs.astrbot.app/)。

## 主要功能

1. 💯 免费 & 开源。
2. ✨ AI 大模型对话，多模态，Agent，MCP，Skills，知识库，人格设定，自动压缩对话。
3. 🤖 支持接入 Dify、阿里云百炼、Coze 等智能体平台。
4. 🌐 多平台，支持 QQ、企业微信、飞书、钉钉、微信公众号、Telegram、Slack 等。
5. 📦 插件扩展，支持一键安装社区插件。
6. 🛡️ [Agent Sandbox](https://docs.astrbot.app/use/astrbot-agent-sandbox.html) 隔离化环境，安全地执行代码、调用 Shell、会话级资源复用。
7. 💻 WebUI 支持。
8. 🌈 Web ChatUI 支持，内置代理沙盒、网页搜索等。
9. 🌐 国际化（i18n）支持。
10. 针对MySQL的特化，防止高并发把sqlite达斯（挨打）
11. 本版本主要针对qq官机特化，其他支持暂未测试
12. <br />

## 内置插件

本分支预置以下插件（位于 `data/plugins/`），均可在 WebUI 面板中查看与配置：

| 插件                                               | 说明                                                       | 作者                                      |
| ------------------------------------------------ | -------------------------------------------------------- | --------------------------------------- |
| **每日鉴毛**（`astrbot_plugin_furrywill`）             | 「每日鉴毛」抽卡指令 + AI 工具（随机抽卡、期数 / 名称 / 地区 / 工作室查询），支持卡片图嵌入回复。 | 星见starcatchere                          |
| **群聊准入（注册码 / 白名单）**（`astrbot_plugin_group_gate`） | 新群需提供注册码或由后台加入白名单才能与机器人对话，未授权群的消息不会进入 AI 对话。             | 星见starcatchere                          |
| **指令上传面板**（`astrbot_plugin_cmd_uploader`）        | 汇总全部已注册指令，在 WebUI 中按需手动上传为 QQ 官方指令面板，配置持久化保存。            | 星见starcatchere                          |
| **记忆压缩与上下文管理**（`astrbot_plugin_memory_ctx`）      | 对话结束后压缩并归档会话记忆、分层保留长期记忆，严格控制单会话上下文长度，并提供记忆面板与检索工具。       | 星见starcatchere                          |
| **情绪表情包**（`emoticon_manager`）                    | 表情包插件，支持情感匹配与 WebUI 管理编辑。                                | @bilibili彩凌，星见starcatchere 二开实现md图片大小调整 |

<br />

## 快速开始

### 本地运行（源码）

> 需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/fishpond-studio/shine-stars.git
cd shine-stars
uv sync
uv run main.py
```

默认会启动 API 服务，监听 `http://localhost:6185`。

### 启动 WebUI（Dashboard）

```bash
cd dashboard
pnpm install   # 仅首次需要；未安装 pnpm 可先执行 npm install -g pnpm
pnpm dev
```

默认运行在 `http://localhost:3000`。

## 部署

本分支与上游保持一致的部署方式，推荐参考官方文档：

- [Docker / Docker Compose 部署](https://docs.astrbot.app/deploy/astrbot/docker.html)

- [手动部署（基于源码与 uv）](https://docs.astrbot.app/deploy/astrbot/cli.html)

- [宝塔面板](https://docs.astrbot.app/deploy/astrbot/btpanel.html) / [1Panel](https://docs.astrbot.app/deploy/astrbot/1panel.html) / [CasaOS](https://docs.astrbot.app/deploy/astrbot/casaos.html)

## 支持的平台与模型

支持 QQ、OneBot v11、Telegram、企业微信、微信公众号、飞书、钉钉、Slack、Discord、LINE、KOOK、Misskey、Mattermost 等消息平台，以及 OpenAI 兼容服务、Anthropic、Google Gemini、DeepSeek、Ollama 等主流模型服务。

完整清单请参考上游文档：[支持的消息平台](https://docs.astrbot.app/) ｜ [支持的模型服务](https://docs.astrbot.app/)。

##

## 许可证与合规

- 本项目基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 修改，遵循 **GNU AGPL-3.0-or-later**，完整条款见 [LICENSE](LICENSE)。

- 对本仓库的修改声明、以及内置第三方插件的来源与许可信息，见 [NOTICE](NOTICE)。

- `data/plugins/` 下的插件均为独立作品，各自保留其原有版权与许可（如 `emoticon_manager` 采用 MIT）。若您是权利人并认为某个插件在未获授权的情况下被分发，请联系维护者以便更正或移除。

- 使用本项目还需遵守 [EULA](EULA.md) 与上游项目相关条款。

## 鸣谢

感谢上游 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 及所有 Contributors 与插件开发者 ❤️

<br />

_陪伴与能力从来不应该是对立面。我们希望创造的是一个既能理解情绪、给予陪伴，也能可靠完成工作的机器人。_

</div>
