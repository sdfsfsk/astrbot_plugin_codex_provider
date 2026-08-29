# astrbot_plugin_codex_provider

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：将 **OpenAI Codex（ChatGPT 订阅）** 作为模型服务提供商接入 AstrBot，使用 Codex CLI 登录获得的 **Access Token** 直接调用 ChatGPT Codex 后端，无需 OpenAI Platform API Key。

> 参考实现：[dsh-codex](https://github.com/Yan-Zero/dsh-codex)（DeepSeek Harness 的 Codex 插件）。

## 功能

- 🔑 **令牌登录**：粘贴 Codex 访问令牌（Access Token）即可使用，自动从 JWT 解析 `chatgpt-account-id`
- 🌐 **代理支持**：默认 `http://127.0.0.1:10809`（v2rayN），可改为 Clash（`7890`）等任意 HTTP/SOCKS 代理，留空则直连；模型请求与订阅查询均走代理
- 🤖 **模型适配**：内置 Codex 模型目录（`gpt-5.6-sol` / `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.3-codex-spark`），WebUI「获取模型列表」直接可用
- 📊 **订阅查询**：`/codex_usage` 命令查询订阅额度（5 小时窗口 / 周窗口 / 附加限额 / 令牌有效期）
- 🧠 **完整能力**：流式输出、工具调用（Function Calling）、推理内容回放（`reasoning.encrypted_content`）、多模态图片输入
- ⏰ **过期提醒**：启动时自动检测令牌有效期并在日志中提醒

## ⚠️ 风险提示

使用 ChatGPT 订阅（Plus/Pro）的 Codex 额度驱动聊天机器人**可能违反 OpenAI 服务条款**，OpenAI 官方对非编码场景使用 Codex 订阅有封号风险。AstrBot 官方也曾因此顾虑撤回过相关 PR（见 [AstrBotDevs/AstrBot#7991](https://github.com/AstrBotDevs/AstrBot/issues/7991)）。**请自行评估风险，建议使用小号。**

## 安装

1. 将本插件目录放入 AstrBot 插件目录 `data/plugins/`，或在 WebUI 插件页通过仓库链接安装；
2. 重启 AstrBot；
3. 打开 WebUI → **服务提供商** → **新增提供商** → 选择 **OpenAI Codex 订阅**。

## 获取令牌（Access Token）

> ⚠️ chatgpt.com 在部分地区无法直连，**登录与获取令牌的全过程建议挂代理**（如 v2rayN、Clash）。

1. 安装并登录官方 Codex CLI：
   ```bash
   npm install -g @openai/codex
   codex login
   ```
2. 登录完成后，打开凭证文件：
   - Linux/macOS：`~/.codex/auth.json`
   - Windows：`C:\Users\<用户名>\.codex\auth.json`
3. 复制其中的 `access_token`（一个很长的 JWT，以 `eyJ` 开头），粘贴到提供商配置的 **Key** 栏；
4. 保存并启用。令牌有过期时间，过期后重新执行 `codex login` 获取新令牌即可（插件启动时会检测并在日志提醒，`/codex_usage` 也会显示有效期）。

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `key` | 空 | Codex 访问令牌（Access Token，`eyJ` 开头的 JWT），支持填多个做轮询 |
| `api_base` | `https://chatgpt.com/backend-api/codex` | Codex 后端地址，一般无需修改 |
| `proxy` | `http://127.0.0.1:10809` | 代理地址。v2rayN 默认 `10809`，Clash 默认 `7890`，支持 `http://` / `socks5://`，留空直连 |
| `model` | `gpt-5.6-sol` | 默认模型，可在 WebUI 切换 |
| `timeout` | `120` | 请求超时（秒） |
| `custom_extra_body` | `{"reasoning_effort": "medium"}` | 自定义请求体参数，用于调整推理深度等 |

> ⚠️ **Key 栏请粘贴 `access_token` 本体**（`eyJ` 开头的长 JWT），不要填 `account_id`、`refresh_token` 等其他字段，否则插件会报「Key 不是有效的访问令牌」。

### 推理深度（reasoning_effort）

在提供商配置的 **自定义请求体参数** 中修改 `reasoning_effort` 即可调整推理深度：

| 取值 | 说明 |
|------|------|
| `minimal` | 最少推理（最快） |
| `low` | 低 |
| `medium` | 中（默认） |
| `high` | 高 |
| `xhigh` | 最高（最慢，额度消耗大） |

## 命令

| 命令 | 说明 |
|------|------|
| `/codex_usage` | 查询 Codex 订阅额度（主要窗口 / 次要窗口 / 附加限额 / 令牌有效期） |

输出示例：

```
🐾 Codex 订阅用量
令牌状态: 有效（到期时间 2026-06-20 11:33）
订阅计划: plus
主要窗口（5小时）: 已用 12% · 剩余 88% · 重置于 06-15 18:30
次要窗口（7天）: 已用 34% · 剩余 66% · 重置于 06-18 09:00
```

## 实现原理

- 通过 AstrBot 的 `register_provider_adapter` 注册 `codex_chat_completion` 提供商类型，插件加载后即出现在 WebUI 提供商列表；
- 复用 AstrBot 内置 Responses API 提供商的消息转换 / 流式 / 工具调用逻辑，仅叠加 Codex 后端要求：
  - 端点 `POST {api_base}/responses`，强制 SSE 流式（`store: false`，非流式入口自动聚合）；
  - 请求头 `Authorization: Bearer <token>`、`chatgpt-account-id`（从令牌 JWT 解析）、`originator: codex_cli_rs`、`OpenAI-Beta: responses=experimental`；
  - 请求体补全 `instructions`、角色规范化（`system` → `developer`）、消息体类型化（`input_text` / `output_text`）；
- 订阅查询走 `GET https://chatgpt.com/backend-api/wham/usage`，与模型请求共用令牌和代理。

## 许可证

[AGPL-3.0](LICENSE)
