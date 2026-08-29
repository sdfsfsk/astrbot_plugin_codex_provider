# astrbot_plugin_codex_provider

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：将 **OpenAI Codex（ChatGPT 订阅）** 作为模型服务提供商接入 AstrBot，使用 Codex CLI 登录获得的 **Access Token** 直接调用 ChatGPT Codex 后端，无需 OpenAI Platform API Key。

> 参考实现：[dsh-codex](https://github.com/Yan-Zero/dsh-codex)（DeepSeek Harness 的 Codex 插件）。

## 功能

- 🔑 **令牌登录**：粘贴 Codex 访问令牌（Access Token）即可使用，自动从 JWT 解析 `chatgpt-account-id`
- 🌐 **代理支持**：默认 `http://127.0.0.1:10808`（v2rayN 混合端口），可改为 Clash（`7890`）等任意 HTTP/SOCKS 代理，留空则直连；模型请求与订阅查询均走代理
- 🤖 **模型适配**：内置 Codex 模型目录（`gpt-5.6-sol` / `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.3-codex-spark`），WebUI「获取模型列表」直接可用
- 📊 **订阅查询**：`/codex_usage` 命令查询订阅额度（5 小时窗口 / 周窗口 / 附加限额 / 令牌有效期）
- 🎚️ **推理深度可调**：插件配置下拉框或 `/codex_reasoning` 指令，五档可选
- 🚀 **1.5 倍速模式**：插件配置或 `/codex_fast` 指令开关 priority 服务层级
- 🧠 **完整能力**：流式输出、工具调用（Function Calling）、推理内容回放（`reasoning.encrypted_content`）、多模态图片输入
- ⏰ **过期提醒**：启动时自动检测令牌有效期并在日志中提醒

## ⚠️ 风险提示

使用 ChatGPT 订阅（Plus/Pro）的 Codex 额度驱动聊天机器人**可能违反 OpenAI 服务条款**，OpenAI 官方对非编码场景使用 Codex 订阅有封号风险。AstrBot 官方也曾因此顾虑撤回过相关 PR（见 [AstrBotDevs/AstrBot#7991](https://github.com/AstrBotDevs/AstrBot/issues/7991)）。**请自行评估风险，建议使用小号。**

## 安装

1. 将本插件目录放入 AstrBot 插件目录 `data/plugins/`，或在 WebUI 插件页通过仓库链接安装；
2. 重启 AstrBot；
3. 打开 WebUI → **服务提供商** → **新增提供商** → 选择 **OpenAI Codex 订阅**。

## 获取令牌（Access Token）

> ⚠️ auth.openai.com 在部分地区无法直连，**登录全过程建议挂代理**（如 v2rayN、Clash）。

### 方式一：`/codex_login` 指令登录（推荐，无需 Codex CLI）

在群聊/私聊中直接发送：

```
/codex_login
```

机器人会回复一个设备码登录链接和验证码，在浏览器打开链接（挂代理）、登录 ChatGPT 账号并输入验证码即可。登录成功后插件会：

- 自动把访问令牌注入 Codex 提供商的 **Key** 栏并重建提供商实例（无需手动填 Key）；
- 保存刷新令牌到 `data/astrbot_plugin_codex_provider/codex_auth.json`，**令牌到期后自动续期**，无需重新登录。

> 说明：采用设备码授权流程，不需要本地回调端口（Windows 上 Codex 默认回调端口可能落在系统保留段内导致无法监听），手机浏览器也能完成授权。

### 方式二：手动粘贴 Codex CLI 令牌

1. 安装并登录官方 Codex CLI：
   ```bash
   npm install -g @openai/codex
   codex login
   ```
2. 登录完成后，打开凭证文件：
   - Linux/macOS：`~/.codex/auth.json`
   - Windows：`C:\Users\<用户名>\.codex\auth.json`
3. 复制其中的 `access_token`（一个很长的 JWT，以 `eyJ` 开头），粘贴到提供商配置的 **Key** 栏；
4. 保存并启用。手动粘贴的令牌有过期时间，过期后重新获取粘贴，或改用 `/codex_login`（支持自动续期）。

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `key` | 空 | Codex 访问令牌（Access Token，`eyJ` 开头的 JWT），支持填多个做轮询 |
| `api_base` | `https://chatgpt.com/backend-api/codex` | Codex 后端地址，一般无需修改 |
| `proxy` | `http://127.0.0.1:10808` | 代理地址。v2rayN 混合端口默认 `10808`（旧版 HTTP 端口为 `10809`），Clash 默认 `7890`，支持 `http://` / `socks5://`，留空直连 |
| `model` | `gpt-5.6-sol` | 默认模型，可在 WebUI 切换 |
| `timeout` | `120` | 请求超时（秒） |
| `custom_extra_body` | `{}` | 自定义请求体参数（如 `temperature` 等），一般无需修改 |

> ⚠️ **Key 栏请粘贴 `access_token` 本体**（`eyJ` 开头的长 JWT），不要填 `account_id`、`refresh_token` 等其他字段，否则插件会报「Key 不是有效的访问令牌」。

### 插件配置（WebUI → 插件管理 → OpenAI Codex 订阅接入 → 插件配置）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `reasoning_effort` | `medium` | Codex 推理深度（下拉框选择），也可用 `/codex_reasoning` 指令调整 |
| `fast_mode` | 关 | 1.5 倍速模式（priority 服务层级），也可用 `/codex_fast` 指令开关 |

### 推理深度（reasoning_effort）

在**插件配置**下拉框选择，或用指令 `/codex_reasoning <级别>` 即时切换（自动保存）：

| 取值 | 说明 |
|------|------|
| `minimal` | 最少推理（最快） |
| `low` | 低 |
| `medium` | 中（默认） |
| `high` | 高 |
| `xhigh` | 最高（最慢，额度消耗大） |

### 1.5 倍速模式（fast_mode）

在**插件配置**开启，或用指令 `/codex_fast on` / `/codex_fast off` 即时开关（自动保存）。开启后以 `priority` 服务层级请求，响应速度约提升 1.5 倍，但订阅额度消耗也更快。

## 命令

| 命令 | 说明 |
|------|------|
| `/codex_login` | 设备码登录 Codex（无需 Codex CLI），成功自动注入提供商 Key，令牌到期自动续期 |
| `/codex_usage` | 查询 Codex 订阅额度（主要窗口 / 次要窗口 / 附加限额 / 令牌有效期） |
| `/codex_reasoning [级别]` | 查看或设置推理深度（minimal/low/medium/high/xhigh） |
| `/codex_fast [on/off]` | 查看或开关 1.5 倍速模式 |

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
- 复用 AstrBot 内置 Responses API 提供商的消息转换 / 工具调用 / 响应解析逻辑，仅叠加 Codex 后端要求：
  - 端点 `POST {api_base}/responses`，强制 SSE 流式（`store: false`，非流式入口自动聚合）；
  - 请求头 `Authorization: Bearer <token>`、`chatgpt-account-id`（从令牌 JWT 解析）、`originator: codex_cli_rs`、`OpenAI-Beta: responses=experimental`；
  - 请求体补全 `instructions`、角色规范化（`system` → `developer`）、消息体类型化（`input_text` / `output_text`）；
  - Codex 后端的 `response.completed` 事件 `output` 恒为空，插件从 `response.output_item.done` 事件流中收集完整输出项并重建最终响应；
- 推理深度 / 倍速模式为插件级配置，每次请求时注入 `reasoning.effort` 与 `service_tier`；
- 订阅查询走 `GET https://chatgpt.com/backend-api/wham/usage`，与模型请求共用令牌和代理。

## 故障排查

| 现象 | 排查 |
|------|------|
| 测试连接失败（连接错误/超时） | 检查代理端口是否真实监听：`netstat -ano \| findstr 1080`。v2rayN 7.x 混合端口是 `10808`，旧版 HTTP 端口是 `10809`，Clash 是 `7890`；填错端口会全部请求失败 |
| 测试连接失败（「Key 不是有效的访问令牌」） | Key 栏误填了 `account_id` 等字段，请粘贴 `eyJ` 开头的 `access_token` 本体 |
| 401/403 | 令牌过期或账号被拒，重新 `codex login` 获取新令牌 |
| 「no usable output」（v1.1.0 前） | Codex 后端 completed 事件 output 为空所致，v1.1.0 已修复，请升级插件 |

## 许可证

[AGPL-3.0](LICENSE)
