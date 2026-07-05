# Selfie Image 开发文档

> 文档重生成日期：2026-07-05
> 重生成基线：`fb96a7c`，`astrbot_plugin_selfie_image` 1.0.0
> 运行形态：AstrBot 插件 + 内置 Flask Web 管理页 + 多 Provider 生图适配器
> 当前回归基线：`tests/test_core.py` 121 个用例；每轮改动后必须重新验证

## 目标与边界

Selfie Image 是 AstrBot 的生图、图生图、AI 自拍、LLM 工具调用和 Web 管理插件。它不是独立 Bot，也不是前端单页项目。所有功能应围绕三条入口保持一致：

| 入口 | 目标 |
|------|------|
| AstrBot 命令 | 面向群聊/私聊用户，提供生图、图生图、自拍、人设和预设命令 |
| LLM 工具 | 允许大模型在会话中按权限、额度和审核规则调用生图能力 |
| Flask Web | 面向管理员，配置渠道、测试模型、管理自拍形象、查看生成记录 |

固定边界：

- 不把真实 `api_key`、Web Token、代理、Cookie、临时签名 URL 写进仓库。
- 不扩大 `_conf_schema.json` 的职责；AstrBot 原生配置只保留 `web.enable`、`web.host`、`web.port`、`web.token`。
- 不在插件启动时覆盖用户完整配置；完整配置只从插件数据目录读取和保存。
- 不绕过 Web Token 鉴权；对外监听时必须拒绝空 Token 和默认弱 Token。
- 不把 Web 面板拆成 npm/Vite/React 等外部构建链，除非单独立项。
- 不提交运行态文件、缓存图片、生成记录、用户配置和真实密钥。
- 保留旧插件名 `astrbot_plugin_aicat` 与旧配置 `aicat_config.json` 的兼容迁移。

## 配置分层

### AstrBot 原生配置

`_conf_schema.json` 只负责插件能否启动 Flask Web 服务：

| 字段 | 说明 |
|------|------|
| `web.enable` | 是否启动内置 Web 服务 |
| `web.host` | Web 监听地址 |
| `web.port` | Web 监听端口 |
| `web.token` | Web 管理 Token |

这些字段由 AstrBot 读取，优先级高于独立配置文件。Web 面板保存配置时必须剔除 `web` 节点，避免运行时配置覆盖启动监听参数。

### 插件独立配置

完整运行配置写入：

```text
plugin_data/astrbot_plugin_selfie_image/selfie_image_config.json
```

加载顺序：

1. 读取 `DEFAULT_CONFIG` 作为基础默认值。
2. 读取 `selfie_image_config.json` 并合并。
3. 兼容旧字段名和旧数据结构。
4. 读取 AstrBot 原生 `web.*` 并覆盖运行态 Web 配置。
5. 构造 `AICatConfig`，供命令、LLM 工具、Web 测试和 provider 调用共用。

核心配置域：

| 配置域 | 内容 |
|--------|------|
| `bot_name`、`personality` | 插件人格与自拍提示词基础信息 |
| `image` | 默认比例、分辨率、并发、超时、缓存上限、额度、频控、审核配置 |
| `permission` | 可用用户、黑名单、白名单用户、白名单群 |
| `image_channels` | 生图渠道列表 |
| `audit_channels` | 审核/OCR 渠道列表 |
| `enabled_image_model_priority` | 已启用模型调用顺序 |

## 数据路径

插件运行数据以 AstrBot 数据目录下的插件目录为根：

```text
plugin_data/astrbot_plugin_selfie_image/
```

| 文件或目录 | 作用 | Git 规则 |
|------------|------|----------|
| `selfie_image_config.json` | Web 保存的完整插件配置 | 不提交 |
| `usage_stats.json` | 每日额度统计 | 不提交 |
| `generation_records.json` | 生成监控记录，最多保留最近记录 | 不提交 |
| `image_cache/` | 请求图、生成图、审核拦截图缓存 | 不提交 |
| `image_persona.json` | 自拍形象与每日状态 | 不提交 |
| `image-persona/` | 自拍参考图文件 | 不提交 |
| `image_presets.json` | 生图预设 | 不提交，除非明确需要示例数据 |

缓存图片读取必须经过路径校验，只允许访问 `image_cache/` 内的合法文件。生成记录和 Web API 输出必须脱敏。

## 功能入口

### AstrBot 命令

| 命令 | 说明 |
|------|------|
| `/生图帮助` | 展示命令和 Web 地址 |
| `/画`、`/生图` | 普通生图，支持预设、数量、参考图和上下文图片 |
| `/文生图` | 原始提示词直通文生图 |
| `/图生图` | 原始提示词直通图生图，必须附带、引用或回溯图片 |
| `/自拍`、`/看看` | 基于 AI 当前形象生成自拍 |
| `/看看腿` | 腿部、穿搭侧重的自拍动作 |
| `/看看你` | 他拍感形象照 |
| `/合影`、`/合照` | 与用户或参考图对象同框 |
| `/形象查看` | 查看自拍参考图和当前状态 |
| `/形象设置` | 保存自拍参考图 |
| `/形象清除` | 清除自拍参考图 |
| `/形象刷新` | 刷新今日自拍设定 |
| `/预设` | 查看预设列表或详情 |
| `/预设添加` | 管理员添加预设 |
| `/预设删除` | 管理员删除预设 |

### LLM 工具

| 工具 | 说明 |
|------|------|
| `generate_image(prompt, count, aspect_ratio, resolution, size, ack_message)` | 普通生图或参考图图生图 |
| `generate_selfie(action, count, aspect_ratio, resolution, size, ack_message)` | 自拍、形象照、换装、合影和同框 |

LLM 工具必须继续遵守 `image.enable_llm_tool`、用户权限、黑白名单、频控、每日额度、提示词审核、出图审核、缓存和记录逻辑。

### Flask Web

默认地址：

```text
http://127.0.0.1:14514
```

已知路由：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/`、`/index.html` | `GET` | 内置 Web 管理页 |
| `/api/health` | `GET` | Web 状态、路径、缓存大小 |
| `/api/config` | `GET`/`POST` | 读取/保存独立配置，不含 `web.*` |
| `/api/selfie-reference` | `GET`/`POST` | 读取/保存自拍参考图 |
| `/api/selfie-reference/clear` | `POST` | 清除自拍参考图 |
| `/api/selfie-profile/refresh` | `POST` | 刷新今日自拍状态 |
| `/api/test-image-channel` | `POST` | 同步渠道测试 |
| `/api/test-image-channel/tasks` | `POST` | 提交后台渠道测试任务 |
| `/api/test-image-channel/tasks/<task_id>` | `GET` | 查询后台测试任务 |
| `/api/refresh-image-models` | `POST` | 刷新渠道模型列表 |
| `/api/records` | `GET` | 查看生成记录，支持 `source`、`model`、`success`、`q`、`offset`、`limit` 筛选分页 |
| `/api/records/<record_id>` | `GET` | 查看单条生成记录详情 |
| `/api/records/clear` | `POST` | 清空生成记录 |
| `/api/cache-image` | `GET` | 读取缓存图片 |

Web API 统一要求：

- JSON POST 请求体必须是 JSON 对象。
- API 响应带 `X-Content-Type-Options: nosniff`。
- API 响应使用 `no-store` 缓存控制。
- 错误文本、任务状态、生成记录必须脱敏。
- 任务 ID、缓存路径、Token 长度和 Token 编码必须校验。

## 核心模块职责

| 模块 | 职责 |
|------|------|
| `constants.py` | 插件名、版本、配置文件名、比例、分辨率、Provider 类型 |
| `models.py` | 默认配置、配置归一化、数据模型、模型目标解析 |
| `main.py` | AstrBot 插件主体、命令、LLM 工具、权限、审核、缓存、记录、Web 生命周期 |
| `web.py` | Flask App、内置 HTML/CSS/JS、鉴权、配置 API、渠道测试 API、记录 API |
| `providers.py` | Provider adapter、模型列表解析、生图请求、响应图片提取和下载 |
| `generator.py` | 多模型 fallback、并发控制、全局超时和失败记录 |
| `persona.py` | 自拍参考图、每日自拍状态、人设提示词与意图解析 |
| `preset.py` | 生图预设读写和管理 |
| `utils.py` | 图片源读取、base64/data URL、图片签名、原子 JSON 保存、脱敏 |
| `tests/test_core.py` | 核心配置、provider、Web API、安全边界和回归测试 |

## Provider 现状

当前支持的 provider 类型：

```text
openai
gemini
gemini_openai
z_image_gitee
jimeng2api
grok
agnes
```

新增或修改 provider 时必须同步检查：

- `constants.py` 的 `PROVIDER_TYPES`。
- `models.py` 的 provider 类型校验、旧字段兼容和模型类型推断。
- `providers.py` 的 adapter 创建、模型列表 URL、请求载荷和响应解析。
- `web.py` 的渠道管理默认值、模型刷新、模型启用顺序和表单读写。
- `tests/test_core.py` 的 URL 归一化、模型推断、响应解析和错误脱敏测试。

响应解析能力应覆盖：

- OpenAI 风格 `b64_json`、`data[].url`、`image_url` 等字段。
- Gemini 原生 `inlineData`、`parts` 和文本中嵌入的图片结果。
- `data:image/...;base64,...`、`base64://...`、URL-safe base64 和常见 base64 字段别名。
- 绝对 URL、协议相对 URL、相对路径、Markdown 图片链接、带标题的链接。
- JSON、代码块 JSON、SSE `data:`、JSONP、`<script>` 内 JSON 和赋值 JSON。
- HTML `img/source src/srcset`、`href`、`meta og:image`、`poster`、`background`、lazy image 属性、`object/embed`。
- CSS `url(...)`。
- URL 转义形式：`\/`、HTML entity、Unicode escape、`\x..`、query 中的转义字符。
- 常见资源字段别名：`url`、`uri`、`path`、`link`、`location`、`imageUrl`、`outputUrl`、`resultUrl`、`signedUrl`、`cdnUrl`、`publicUrl`、`previewUrl`、`thumbnailUrl`、`fileUrl`、`assetUrl`、`downloadUrl` 等。

下载远程图片时必须同时校验响应大小、Content-Type 和文件签名。Content-Type 不可信时以图片签名为最终依据；非图片内容不能写入缓存。

## 安全与鉴权

Web 鉴权规则：

- 配置了 Token 时，API 必须通过 `Authorization: Bearer ...`、`X-Selfie-Image-Token` 或兼容参数提交 Token。
- Token 比较使用常量时间比较。
- 非 ASCII 或超长 Token 请求必须拒绝。
- 空 Token 只允许本机监听地址。
- 默认弱 Token `changeme` 不允许在 `0.0.0.0`、`::` 等对外监听地址上使用。

敏感信息处理：

- 错误消息、生成记录、任务状态、provider fallback 尝试信息必须调用脱敏逻辑。
- 重点脱敏对象包括 Bearer Token、OpenAI/xAI/Gemini 风格 key、`api_key`、`token=`、`authorization`、`cookie`、签名查询参数等。
- Web 返回值不得泄露完整密钥、真实请求头或用户私有配置。

## 开发约束

- 搜索优先用 `rg`，文件列表优先用 `rg --files`。
- 手工编辑使用 `apply_patch`。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令，除非用户明确要求。
- 不回滚用户未提交改动；如果工作区已脏，只提交本轮相关文件。
- 不 amend 既有提交，除非用户明确要求。
- 修改配置项时，先更新 `DEFAULT_CONFIG` 和归一化，再更新 Web 读写，最后补测试。
- 修改 provider 时，优先补响应解析测试，再实现 adapter 或解析逻辑。
- 修改 Web API 时，必须覆盖鉴权、非法 JSON、错误脱敏和路径校验。
- 修改 Web UI 时保持内置单文件可运行，避免引入新构建链。
- 修改图片读写时必须验证大小上限、图片签名、缓存清理和路径穿越防护。

## 每轮验收

必须运行：

```sh
python -m unittest tests/test_core.py
python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py
git diff --check
```

涉及 shell 脚本时额外运行：

```sh
sh -n grok_image_edit_batch.sh
```

涉及 Web 行为时，至少验证对应 Flask test client 或本地 Token API。涉及 provider 解析时，至少补一条单元测试覆盖真实失败形态。

## 近期变更摘要

### 2026-07-03

- 搭建插件核心结构：配置模型、provider adapter、多模型 fallback、AstrBot 命令、LLM 工具和基础 Web 面板。
- 建立独立配置文件与旧插件数据迁移路径。
- 建立核心单元测试，覆盖配置归一化、模型优先级、基础 provider 行为和缓存工具。

### 2026-07-04

- 强化图片缓存、生成记录、请求图/生成图保存、缓存上限清理。
- 扩展 Web API：配置保存、渠道测试、后台任务、记录查看、缓存图片读取。
- 增加 Web API 的非法 JSON、任务 ID、缓存路径和记录清理边界测试。

### 2026-07-05

- 持续增强 provider 响应解析：相对 URL、HTML、Markdown、SSE、JSONP、script JSON、CSS URL、lazy image、资源字段别名和多种转义 URL。
- 强化下载图片校验：大小、Content-Type、图片签名和非图片拒绝。
- 强化 Web 鉴权：弱 Token、空 Token、本机监听、非 ASCII Token、超长 Token、API no-store 和 nosniff。
- 强化错误和记录脱敏，避免 provider 错误、任务状态和 Web 响应泄露密钥。
- 重新生成本文档，删除长流水式更新记录，改为当前架构、边界、验收和近期主题摘要。

### 2026-07-06

- 补充 Web API 细粒度回归测试，覆盖配置读取/保存、模型刷新成功/失败和渠道测试后台任务响应。
- 收紧同步渠道测试和后台任务创建响应脱敏，避免成功响应体泄露 provider 密钥、Token 或认证头。
- 配置保存异常改为统一 JSON 错误响应，并沿用敏感信息脱敏。
- 继续补充自拍参考图保存、模型启用顺序更新和缓存图片非图片文件拒绝测试。
- 缓存图片读取会显式校验图片签名，拒绝缓存目录内的非图片文件。
- 增加生成记录详情 API，Web 详情弹窗按记录 ID 拉取后端脱敏后的完整记录。
- 补充记录详情鉴权、非法 ID、404、脱敏、自拍参考图清除后状态和模型启用/停用组合测试。
- 增加生成记录列表后端筛选和分页参数，并覆盖默认兼容、分页筛选和非法参数测试。
- Web 监控页改为使用后端记录筛选分页，避免记录较多时全量加载到浏览器。

## 下一步建议

1. 将 `providers.py` 的响应解析拆成独立 parser 模块，降低 adapter 文件体积。
2. 继续扩展 Web API 的 Flask test client 用例，覆盖更多模型启用顺序组合和 Web 前端异常状态。
3. 增加一次真实 AstrBot 环境冒烟检查，确认命令、LLM 工具和 Web 配置热更新在运行时一致。
4. 整理 `web.py` 内置前端结构，在不引入构建链的前提下分区收敛状态管理和重复渲染逻辑。
