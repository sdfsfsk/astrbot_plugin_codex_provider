# astrbot_plugin_codex_provider

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：将 **OpenAI Codex（ChatGPT 订阅）** 作为模型服务提供商接入 AstrBot，使用 Codex CLI 登录获得的 **Access Token** 直接调用 ChatGPT Codex 后端，无需 OpenAI Platform API Key。

> 参考实现：[dsh-codex](https://github.com/Yan-Zero/dsh-codex)（DeepSeek Harness 的 Codex 插件）。

## 功能

- **安全 OAuth 登录**：管理员私聊执行 `/codex_login`，凭据写入 AstrBot 插件数据目录并自动续期；也兼容手动 Access Token
- **代理支持**：默认 `http://127.0.0.1:10808`（v2rayN 混合端口），可改为 Clash（`7890`）等任意 HTTP/SOCKS 代理，留空则直连；模型请求与订阅查询均走代理
- **模型适配**：内置 Codex 模型目录（`gpt-5.6-sol` / `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.3-codex-spark`）+ 在线拉取官方增量模型（`GET /codex/models`），官方上新模型自动出现；WebUI「获取模型列表」直接可用
- **订阅查询**：`/codex_usage` 命令查询订阅额度（5 小时窗口 / 周窗口 / 附加限额 / 令牌有效期）
- **图片生成与编辑**：`/codex_image` 指令 + LLM 工具 `codex_generate_image`，调用订阅内 `gpt-image-2`；当前消息或引用消息带图时自动走编辑端点，支持最多 5 张有效参考图
- **联网搜索**：LLM 工具 `codex_web_search` 调用 Codex 自带搜索（`alpha/search`），支持实时/索引/缓存三种模式；插件配置可一键强制禁用 AstrBot 自带联网
- **推理深度可调**：插件配置下拉框或 `/codex_reasoning` 指令，五档可选
- **1.5 倍速模式**：插件配置或 `/codex_fast` 指令开关 priority 服务层级
- **完整能力**：流式输出、工具调用（Function Calling）、推理内容回放（`reasoning.encrypted_content`）、多模态图片输入
- **过期提醒**：启动时自动检测令牌有效期并在日志中提醒

## 风险提示

使用 ChatGPT 订阅（Plus/Pro）的 Codex 额度驱动聊天机器人**可能违反 OpenAI 服务条款**，OpenAI 官方对非编码场景使用 Codex 订阅有封号风险。AstrBot 官方也曾因此顾虑撤回过相关 PR（见 [AstrBotDevs/AstrBot#7991](https://github.com/AstrBotDevs/AstrBot/issues/7991)）。**请自行评估风险，建议使用小号。**

## 安装

1. 将本插件目录放入 AstrBot 插件目录 `data/plugins/`，或在 WebUI 插件页通过仓库链接安装；
2. 重启 AstrBot；
3. 打开 WebUI → **服务提供商** → **新增提供商** → 选择 **OpenAI Codex 订阅**。

## 获取令牌（Access Token）

> 注意：auth.openai.com 在部分地区无法直连，**登录全过程建议挂代理**（如 v2rayN、Clash）。

### 方式一：`/codex_login` 指令登录（推荐，无需 Codex CLI）

请使用 AstrBot 管理员账号在**私聊**中发送，群聊会拒绝展示设备码：

```
/codex_login
```

机器人会回复一个设备码登录链接和验证码，在浏览器打开链接（挂代理）、登录 ChatGPT 账号并输入验证码即可。登录成功后插件会：

- 将刷新令牌保存到 `data/plugin_data/astrbot_plugin_codex_provider/codex_auth.json` 的受保护凭据库；为兼容 AstrBot WebUI，会把短期 Access Token 自动填入空 Key 或旧 OAuth 来源；
- 通过不可逆 token 指纹跟踪 OAuth 轮换历史，仅更新/移除插件管理的旧令牌副本，手工多账号 Key 保持不变；**令牌到期自动续期**，可用 `/codex_logout` 安全退出。

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
| `key` | 空 | 推荐留空并使用 `/codex_login`，成功后会自动填入短期 Access Token；也可手动填写 `eyJ` 开头的令牌，手工多账号 Key 不会被 OAuth 登录覆盖 |
| `api_base` | `https://chatgpt.com/backend-api/codex` | OAuth 安全边界，固定为该官方 HTTPS 地址；需要代理时请配置 `proxy` |
| `proxy` | `http://127.0.0.1:10808` | 代理地址。v2rayN 混合端口默认 `10808`（旧版 HTTP 端口为 `10809`），Clash 默认 `7890`，支持 `http://` / `socks5://`，留空直连 |
| `model` | `gpt-5.6-sol` | 默认模型，可在 WebUI 切换 |
| `timeout` | `120` | 请求超时（秒） |
| `custom_extra_body` | `{}` | 自定义请求体参数（如 `temperature` 等），一般无需修改 |

> 注意：**Key 栏请粘贴 `access_token` 本体**（`eyJ` 开头的长 JWT），不要填 `account_id`、`refresh_token` 等其他字段，否则插件会报「Key 不是有效的访问令牌」。

### 插件配置（WebUI → 插件管理 → OpenAI Codex 订阅接入 → 插件配置）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `reasoning_effort` | `medium` | Codex 推理深度（下拉框选择），也可用 `/codex_reasoning` 指令调整 |
| `fast_mode` | 关 | 1.5 倍速模式（priority 服务层级），也可用 `/codex_fast` 指令开关 |
| `enable_search_tool` | 开 | 注册 `codex_web_search` LLM 联网搜索工具 |
| `force_codex_web_search` | 关 | 开启后自动禁用 AstrBot 自带联网搜索（`provider_settings.web_search`），关闭时自动恢复原值 |
| `search_mode` | `live` | 搜索模式：live 实时抓取 / indexed 仅索引 / cached 仅缓存 |
| `search_context_size` | `medium` | 搜索结果规模：low / medium / high |
| `image_quality` | `auto` | gpt-image-2 质量档位；改图追求画风一致建议锁 `high` |

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
| `/codex_login` | 管理员私聊设备码登录，凭据进入受保护存储并自动续期 |
| `/codex_logout` | 管理员删除 OAuth 凭据及其旧配置副本，保留其他手工 Key |
| `/codex_usage` | 管理员查询订阅额度（主要窗口 / 次要窗口 / 附加限额 / 令牌有效期） |
| `/codex_reasoning [级别]` | 管理员查看或设置推理深度（minimal/low/medium/high/xhigh） |
| `/codex_fast [on/off]` | 管理员查看或开关 1.5 倍速模式 |
| `/codex_image <描述>` | 用 gpt-image-2 生成图片；消息附加/引用图片时为改图模式（最多 5 张参考图） |

另注册 LLM 工具 `codex_generate_image` 与 `codex_web_search`，LLM 可在对话中自主调用生成图片、编辑当前或引用消息中的图片、联网搜索。`codex_generate_image` 默认使用消息内参考图；仅当用户明确要求忽略附图并从零生成时，才传入 `use_reference_images=false`。

### 图片编辑说明

- 消息中有可读取的当前图片或引用图片时，命令和 LLM 工具都会调用 `/images/edits`；没有参考图时才调用 `/images/generations`。
- 如果消息带图但全部读取失败，插件会取消任务并明确报错，不再静默降级为文生图。
- `gpt-image-2` 会自动以高保真方式处理参考图，不支持额外设置 `input_fidelity`；`image_quality=high` 提升输出质量，但不等于锁定未编辑区域的像素。
- ChatGPT 网页版还可能使用未公开的提示改写、资产状态或区域编辑编排，当前 Codex 订阅端点不能保证与网页结果逐像素一致；若必须保证遮罩外像素完全不变，需要另行实现遮罩或局部区域合成流程。

### v1.5 安全与协议加固

- OAuth Bearer 只能发送到固定的 `https://chatgpt.com/backend-api/codex`，拒绝自定义主机、HTTP、userinfo、非标准端口和查询参数。
- 凭据采用严格版本格式、跨进程文件锁、随机临时文件、flush/fsync 与原子替换；Windows 使用当前用户 DPAPI 加密，POSIX 文件权限限制为 `0600`。
- 多模型实例共享刷新协调器，锁内核对 access token 与账号，防止 refresh token 轮换竞争或跨账号串用。
- 支持 `response.done`、嵌套 SSE error、流关闭、截断响应拒绝、terminal quota 不重试，以及会话 prompt cache key。
- LLM 工具 schema 现在声明必填参数、`additionalProperties=false` 和参考图默认值；搜索引用会转换成可点击 URL。
- 插件禁用或卸载时会取消登录轮询、恢复 AstrBot 搜索、终止 Codex provider 实例并注销 provider 类型，重新启用时自动恢复。
- 图片输入、输出、提示词和搜索词均有边界限制；生成图片写入 AstrBot managed temp，由框架统一清理。

> 运行边界：一个 `ASTRBOT_ROOT` 只应由一个 AstrBot 进程使用。文件锁与 CAS 可防止跨进程旧响应覆盖 login/logout，但 OAuth 网络刷新 single-flight 只在单进程内保证；多开实例请使用各自独立的数据根目录。

`/codex_usage` 输出示例：

```
Codex 订阅用量
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
  - Codex 后端的终止事件可能是 `response.completed` 或 `response.done`，插件从 `response.output_item.done` 收集完整输出项并重建最终响应；
- 推理深度 / 倍速模式为插件级配置，每次请求时注入 `reasoning.effort` 与 `service_tier`；
- 订阅查询走 `GET https://chatgpt.com/backend-api/wham/usage`，与模型请求共用令牌和代理；
- 图片生成走 `{api_base}/images/generations`（纯生成）与 `{api_base}/images/edits`（带参考图），模型固定 `gpt-image-2`；
- 联网搜索走 `{api_base}/alpha/search`，即 Codex 客户端内置的独立搜索协议。

## 故障排查

| 现象 | 排查 |
|------|------|
| 测试连接失败（连接错误/超时） | 检查代理端口是否真实监听：`netstat -ano \| findstr 1080`。v2rayN 7.x 混合端口是 `10808`，旧版 HTTP 端口是 `10809`，Clash 是 `7890`；填错端口会全部请求失败 |
| 测试连接失败（「Key 不是有效的访问令牌」） | Key 栏误填了 `account_id` 等字段，请粘贴 `eyJ` 开头的 `access_token` 本体 |
| 401/403 | 令牌过期或账号被拒，重新 `codex login` 或发送 `/codex_login` 获取新令牌 |
| 「no usable output」（v1.1.0 前） | Codex 后端 completed 事件 output 为空所致，v1.1.0 已修复，请升级插件 |

## 开发验证

测试需要 AstrBot 源码环境以及 `pytest`、`pytest-asyncio`，当前仓库回归范围覆盖 OAuth store/刷新、安全端点、Responses SSE、工具 schema、搜索策略、引用和图片编辑：

```powershell
$env:PYTHONPATH = "H:\\path\\to\\AstrBot"
python -m pytest -p no:cacheprovider -q tests
```

## 许可证

[AGPL-3.0](LICENSE)
