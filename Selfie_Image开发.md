# Selfie Image 开发方案

> **当前产品基线**：Selfie Image 生图自拍 `1.0.0`，仓库 `astrbot_plugin_selfie_image`，当前 HEAD `59a0dce`。
> **插件名**：`astrbot_plugin_selfie_image`。
> **本文**：`astrbot_plugin_selfie_image/Selfie_Image开发.md`，本地开发方案；默认不提交 Git，除非用户明确要求。
> **运行形态**：AstrBot 插件 + Flask Web 管理页 + 多渠道图片生成适配器。
> **最低回归规则**：改动后至少跑 `python -m unittest tests/test_core.py`；涉及脚本时跑 `sh -n grok_image_edit_batch.sh`。

## 当前结论

Selfie Image 当前定位是 **AstrBot 生图 / 图生图 / AI 自拍 / LLM 工具调用 / Flask Web 管理插件**。它不是独立 Bot，也不是前端单页项目；运行入口由 AstrBot 插件系统加载，Web 面板由插件内部 Flask 服务提供。

一句话目标：保持 AstrBot 命令、LLM 工具和 Web 管理页三条入口一致，让用户能稳定配置渠道、生成图片、管理自拍形象、查看监控记录，并且不把敏感配置写进仓库。

## 当前主线：生图插件可靠性

后续开发围绕四个问题展开：

| 问题 | 目标 |
|------|------|
| 渠道怎么更稳？ | 多 provider、多模型优先级、fallback、超时和错误记录可解释 |
| 配置怎么不丢？ | AstrBot 原生配置只放 Web 启动项，完整插件配置独立持久化 |
| 图片怎么可追踪？ | 请求图、生成图、监控记录和缓存清理都有明确路径和上限 |
| 用户入口怎么一致？ | 命令、LLM 工具、Web 测试走同一套配置、审核、额度和生成链路 |

## 固定边界

- **不把生图渠道密钥写进仓库**：`api_key`、Web Token、代理等只能在运行配置或环境中出现，不能进入 README 示例以外的真实配置。
- **不扩大 `_conf_schema.json` 的职责**：AstrBot 原生配置只保留 `web.enable`、`web.host`、`web.port`、`web.token`；渠道、模型、人设、权限、审核等完整配置由 Web 面板写入插件数据目录。
- **不在启动时覆盖用户配置**：`main.py` 当前刻意避免启动时写默认配置，后续不能恢复“启动即保存默认值”的行为。
- **不绕过 Web Token 鉴权**：对外监听时必须配置强 Token；Token 为空只允许本机监听地址免校验。
- **不拆散核心生成链路**：命令、LLM 工具和 Web 渠道测试都应复用配置模型、provider adapter、审核、缓存和记录逻辑。
- **不把 Web 面板改成外部构建项目**：当前 `web.py` 内置 HTML/CSS/JS，除非专门立项，不引入前端构建链。
- **不破坏旧数据迁移**：保留 `astrbot_plugin_aicat` 数据目录和 `aicat_config.json` 到新路径的兼容迁移。
- **不提交生成图片、缓存、监控记录和真实配置**：`image_cache`、`generation_records.json`、`usage_stats.json`、`selfie_image_config.json` 属于运行数据。

## 工作区结构

| 路径 | 作用 |
|------|------|
| `main.py` | AstrBot 插件主体、命令、LLM 工具、配置加载、生成流程、审核、缓存和记录 |
| `web.py` | 内置 Flask Web UI、Token 鉴权、配置/渠道/测试/监控/缓存 API |
| `models.py` | 默认配置、配置归一化、数据模型、渠道目标解析 |
| `providers.py` | 各生图 provider adapter、图片 URL/二进制解析、接口兼容逻辑 |
| `generator.py` | 多模型 fallback、重试、全局超时控制 |
| `persona.py` | 自拍形象参考图、每日自拍状态、人设和意图解析 |
| `preset.py` | 生图预设管理 |
| `utils.py` | 图片读取、base64/data URL、事件文本/图片源解析、JSON 原子保存 |
| `_conf_schema.json` | AstrBot 原生配置 schema，仅保留 Web 启动项 |
| `metadata.yaml` | AstrBot 插件元信息 |
| `requirements.txt` | 运行依赖：`aiohttp`、`Flask`、`Werkzeug` |
| `tests/test_core.py` | 当前单元测试 |
| `grok_image_edit_batch.sh` | 独立 xAI Grok 批量图生图辅助脚本，不是插件运行必需链路 |

## 配置和数据路径

插件启动时会以 AstrBot 数据目录为根：

```text
plugin_data/astrbot_plugin_selfie_image/
```

关键文件：

| 文件/目录 | 说明 |
|-----------|------|
| `selfie_image_config.json` | Web 面板保存的完整插件配置，不包含 AstrBot 启动用 Web host/port/token |
| `usage_stats.json` | 每日用户生图额度统计 |
| `generation_records.json` | 最近生成记录，当前最多保留 100 条 |
| `image_cache/` | 请求图、生成图和审核拦截图缓存 |
| `image_persona.json` | 自拍形象配置和每日状态 |
| `image-persona/` | 自拍形象参考图文件 |
| `image_presets.json` | 生图预设 |

配置加载规则：

1. 从 AstrBot 原生配置读取 `web.*`。
2. 从 `selfie_image_config.json` 读取完整运行配置。
3. 使用 `DEFAULT_CONFIG` 补齐缺失项。
4. `web.*` 始终以 AstrBot 原生配置为准。
5. Web 面板保存配置时会主动移除 `web`，避免覆盖启动监听参数。

## 功能入口

### AstrBot 命令

| 命令 | 说明 |
|------|------|
| `/生图帮助` | 查看命令和 Web 地址 |
| `/画`、`/生图` | 普通生图，支持预设、数量、参考图、上下文图片 |
| `/文生图` | 原始提示词直通文生图 |
| `/图生图` | 原始提示词直通图生图，必须附带/引用图片或可回溯上下文图片 |
| `/自拍`、`/看看` | 使用 AI 当前形象生成自拍 |
| `/看看腿` | 腿部/穿搭侧重的自拍动作封装 |
| `/看看你` | 他拍感形象照，不是手持自拍 |
| `/合影`、`/合照` | 与用户或参考图对象同框 |
| `/形象查看` | 查看自拍形象参考图和当前状态 |
| `/形象设置` | 保存自拍参考图 |
| `/形象清除` | 清除自拍参考图 |
| `/形象刷新` | 刷新今日自拍设定 |
| `/预设` | 查看预设列表或详情 |
| `/预设添加` | 管理员添加预设 |
| `/预设删除` | 管理员删除预设 |

### LLM 工具

| 工具 | 说明 |
|------|------|
| `generate_image(prompt, count, aspect_ratio, resolution, size, ack_message)` | 普通生图/参考图图生图 |
| `generate_selfie(action, count, aspect_ratio, resolution, size, ack_message)` | 自拍、形象照、换装、合影和同框 |

LLM 工具必须继续遵守：

- `image.enable_llm_tool` 开关。
- 用户额度和频控。
- 白名单/黑名单。
- 提示词审核和出图审核。
- 图片缓存和生成记录。

### Flask Web

默认地址：

```text
http://127.0.0.1:14514
```

主要页面：

- 基础设置。
- 渠道管理。
- 渠道监控。
- 渠道测试。
- 生图设置。
- 形象设置。
- 生图审核。
- JSON 兜底编辑。

主要 API：

| API | 说明 |
|-----|------|
| `GET /api/health` | Web 状态、配置路径、记录路径、缓存大小 |
| `GET/POST /api/config` | 读取/保存 Web 管理配置，不含 `web.*` |
| `GET/POST /api/selfie-reference` | 读取/保存自拍参考图 |
| `POST /api/selfie-reference/clear` | 清除自拍参考图 |
| `POST /api/selfie-profile/refresh` | 刷新今日自拍状态 |
| `POST /api/test-image-channel` | 同步渠道测试 |
| `POST /api/test-image-channel/tasks` | 后台提交渠道测试任务 |
| `GET /api/test-image-channel/tasks/<task_id>` | 查询后台测试任务 |
| `POST /api/refresh-image-models` | 刷新渠道模型列表 |
| `GET /api/records` | 查看生成记录 |
| `POST /api/records/clear` | 清空生成记录 |
| `GET /api/cache-image?path=...` | 查看缓存图片 |

## Provider 边界

当前支持渠道类型：

- `openai`
- `gemini`
- `gemini_openai`
- `z_image_gitee`
- `jimeng2api`
- `grok`
- `agnes`

新增 provider 时必须同时考虑：

- `constants.py` 中的 `PROVIDER_TYPES`。
- `models.py` 的 provider 类型校验和模型类型推断。
- `providers.py` 的 adapter 创建和请求/响应解析。
- Web 渠道管理里的默认 base URL、模型缓存和启用模型顺序。
- `tests/test_core.py` 里的模型类型推断或 URL 归一化测试。

## 审核、权限和额度

命令、LLM 工具和 Web 测试应统一经过：

- 用户黑名单。
- 可使用人员白名单。
- 白名单用户/群组豁免额度和审核。
- 用户冷却时间。
- 每日额度。
- 提示词屏蔽词。
- 提示词审核模型。
- 出图审核模型。
- OCR / 识图模型。

注意：Web 面板可测试渠道，但仍需要 Token 鉴权。不要为了调试直接开放无鉴权测试接口。

## Web UI 约束

- 当前 Web UI 是内嵌 `INDEX_HTML`，修改时保持单文件可运行。
- 不引入 npm、Vite、React 等构建链，除非单独决定重构。
- 表单项要和 `DEFAULT_CONFIG`、Web 读写逻辑保持一致。
- 新增配置项必须能在 `update_config_from_web()` 后立即更新运行态。
- JSON 页面是兜底编辑，不应成为唯一配置入口。

## 开发顺序

1. 先跑当前回归：`python -m unittest tests/test_core.py`。
2. 阅读实际入口：命令改动看 `main.py`，Web 改动看 `web.py`，渠道改动看 `providers.py` 和 `generator.py`。
3. 小范围修改，优先复用现有 `AICatConfig`、`ImageGenerateRequest`、`ImageModelTarget`。
4. 如果新增配置，先改 `DEFAULT_CONFIG` 和模型归一化，再改 Web 表单/API，最后补测试。
5. 如果新增 provider，先补 adapter 和目标解析，再补 Web 渠道管理，最后补测试。
6. 如果涉及图片文件，验证缓存上限、记录路径和清理逻辑。
7. 跑最低回归；涉及 Web 时至少做一次本地 Token API 检查。
8. 更新本文“最后更新”或新增阶段记录。

## 每轮必守验收

- `python -m unittest tests/test_core.py` 通过。
- Python 文件语法检查通过：

```sh
python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py
```

- 涉及 shell 脚本时：

```sh
sh -n grok_image_edit_batch.sh
```

- `_conf_schema.json` 仍只包含 `web.enable`、`web.host`、`web.port`、`web.token`。
- `README.md` 的使用步骤和命令说明没有和实际命令脱节。
- 不出现真实 `api_key`、Token、Cookie、代理账号。
- Web API 未新增无鉴权写接口。
- 启动流程不会覆盖 `selfie_image_config.json`。
- 改 provider 后至少验证一个成功响应解析和一个错误响应预览。

## 搜索与验证命令

快速查看插件入口：

```sh
rg -n "@filter.command|@LLM_TOOL|def _run_image_generation|web_test_image|start_web_image_task" main.py
```

检查配置分层：

```sh
rg -n "_conf_schema|DEFAULT_CONFIG|selfie_image_config|update_config_from_web|_persist_config|web\\.token|web_token" .
```

检查 Web 鉴权：

```sh
rg -n "check_auth|token_from_request|Unauthorized|@app.route" web.py
```

检查敏感信息残留：

```sh
rg -n "sk-[A-Za-z0-9]|api_key\\s*[:=]\\s*[\"'][^\"']{8,}|Bearer\\s+[A-Za-z0-9._-]{16,}|changeme" .
```

运行单测：

```sh
python -m unittest tests/test_core.py
```

## 本地 Web 手动验证

在 AstrBot 环境里启用插件后：

1. 确认日志输出 Flask Web 地址。
2. 打开 `http://127.0.0.1:14514`。
3. 用 `web.token` 登录。
4. 在渠道管理添加测试渠道。
5. 刷新模型缓存。
6. 启用模型并保存。
7. 用渠道测试生成一张图。
8. 在渠道监控里查看请求、响应、生成图和来源。
9. 确认缓存目录大小没有超过上限后失控增长。

命令侧最小回归：

- `/生图帮助`
- `/画 一只白猫坐在窗边 --ar 1:1`
- `/文生图 一只白猫坐在窗边 --ar 1:1`
- `/图生图 改成黄昏暖光 --ar 1:1`，附带或引用图片。
- `/形象设置` 附带图片。
- `/形象查看`
- `/自拍 看着镜头自然自拍 --ar 3:4`
- `/预设`

## 开发收尾规则

每轮 Selfie Image 开发结束前，至少记录：

- 改了哪些模块。
- 是否新增/修改配置项。
- 是否影响命令、LLM 工具或 Web API。
- 是否影响 provider 请求/响应解析。
- 是否影响图片缓存、生成记录或用户额度。
- 跑了哪些测试命令，结果是什么。
- 是否需要 AstrBot 真机/实环境复测。

不要只说“已修复”；必须留下路径、命令和可复查边界。

## 暂缓事项

- 把 Web UI 拆成独立前端工程。
- 把完整渠道配置塞回 `_conf_schema.json`。
- 引入数据库替代当前 JSON 文件。
- 自动上传、同步或提交用户生成图片。
- 自动提交 Git 或发布版本。
- 在无 AstrBot 环境下重写一个独立 Bot 运行层。

## 当前下一步

- 在 AstrBot 实际插件目录中安装依赖并启用插件。
- 配置强 Web Token。
- 通过 Web 添加至少一个生图渠道和一个审核渠道。
- 跑一次 Web 渠道测试和一次命令侧 `/画`。
- 如需继续开发，优先补更完整的 provider 单元测试和 Web API 轻量测试。

## 开工口令

- “继续 Selfie Image” -> 先跑 `python -m unittest tests/test_core.py`，再看本文件确认边界。
- “改生图渠道” -> 先看 `providers.py`、`generator.py`、`models.py`，同步补 provider 测试。
- “改 Web 面板” -> 先看 `web.py` 的 `INDEX_HTML` 和 `/api/config`，确认不会破坏 Token 鉴权。
- “改配置” -> 先确认配置属于 AstrBot 启动项还是插件运行项；启动项才进 `_conf_schema.json`。
- “查图片缓存” -> 先看 `image_cache/`、`generation_records.json` 和 `_cleanup_image_cache_if_needed()`。

## 最后更新

2026-07-03：建立 Selfie Image 本地开发方案，明确插件定位、配置分层、Web 鉴权、provider 边界、缓存记录和最低验收命令。

2026-07-03：补充自动化回归覆盖：provider 通用响应解析、Grok payload、Agnes 参考图 payload/错误预览、Web Token 鉴权、Web 配置隔离、`_conf_schema.json` 启动项边界。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：继续补自动化回归覆盖：多模型 fallback 尝试记录、无模型错误、provider URL 提取/清理、Gemini base URL 归一化、Web records API 鉴权、cache-image 正常读取和路径穿越拒绝。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：补充配置模型优先级回归，验证手动模型 provider 类型、优先级顺序和禁用渠道不会进入生成目标；新增 `.gitignore`，避免提交 `__pycache__`、运行配置、使用统计、生成记录、图片缓存和自拍形象文件。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：修复 Web 刷新模型列表时 Gemini base URL 归一化错误，Gemini 渠道会从 `.../v1beta/...` 生图端点还原到 API 根并优先请求 `/v1beta/models`，同时复用 provider 别名归一化；OpenAI 兼容渠道保持原候选顺序。补充 `build_model_list_urls()` 单测。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：增强 Web 刷新模型列表的响应解析，除 `id/name` 外支持 `model`、`model_id`、`modelName`、`model_name` 等常见字段，并避免把 `owner/object/metadata` 等无关字符串扫入模型缓存。补充 `extract_model_ids_from_response()` 单测。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：增强审核模型响应解析，抽出 `parse_audit_response_text()` 纯函数，支持 fenced JSON、`safe/is_safe`、`unsafe/risk/flagged`、`result/status/verdict` 等常见字段，避免 `{"safe": true, "risk": false}` 被文本 fallback 里的 `false` 误判为拒绝。补充 JSON 和纯文本审核解析单测。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：补齐 Web 刷新模型列表的渠道类型兼容读取，`providerType`、`api_type`、`apiType` 和 `google/xai/openai_compatible` 等别名会按现有 provider 归一化处理，避免 JSON 兜底编辑或旧字段传入时误按 OpenAI 刷新。补充 `provider_type_from_channel_payload()` 单测。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：增强 AstrBot provider 审核 fallback 的返回值处理，新增 `resolve_awaitable()`，可解析普通值、Future、单层/嵌套 awaitable，避免不同 AstrBot provider SDK 返回形态导致审核 fallback 丢结果。补充异步工具单测。验证命令：`python -Wd -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-03：补充 Web API 轻量回归和 provider 错误预览兼容。Web JSON POST 接口会在收到数组等非对象请求体时返回 400，`/api/config` 的 `config` 包装字段也必须是对象，避免无效请求体进入插件逻辑后变成 500；provider HTTP 错误预览新增 `{"error":"..."}` 和 `{"detail":"..."}` 提取。补充相关单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强渠道监控记录清理，`clear_recent_records()` 清空记录时会同步删除记录引用的请求图、生成图和旧版 `image_paths` 缓存文件；删除逻辑只允许移除 `image_cache` 内相对路径，路径穿越会被跳过。新增 `collect_record_cache_paths()` 和 `safe_delete_relative_files()` 工具函数及单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider 通用响应解析，支持从 `url/image/image_url/output` 等字段识别相对图片路径，并按实际请求 base URL 解析下载，覆盖 OpenAI 兼容和 Agnes 等返回 `/outputs/xxx.png` 的代理服务。补充通用解析器和 Agnes adapter 相对 URL 下载单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强图片 URL 下载容错，`Content-Length` 非标准或不可解析时不再直接丢弃响应，而是继续按流式下载字节数和图片签名校验；合法且超限的长度头仍会提前拒绝。补充非法 `Content-Length` 回归测试。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧监控记录缓存清理工具的安全边界，`safe_delete_relative_files()` 在 base 目录为空时直接跳过，并拒绝删除绝对路径输入，只处理明确位于缓存目录内的相对路径。扩展缓存清理单测覆盖绝对路径和空 base 场景。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强图片 URL 下载 Content-Type 兼容，除 `image/*` 和 `application/octet-stream` 外，额外放行 `binary/octet-stream`、`application/binary`、`application/x-binary`，适配部分代理/CDN 的二进制图片响应；仍继续依赖图片签名校验避免误收非图片内容。补充二进制类型别名单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强参考图 base64 解析容错，`data_url_to_bytes()` 对 malformed `data:image/...;base64,...`、`base64://...` 和纯 base64 输入不再抛解码异常，而是返回空数据让上层走现有无效图片错误路径。补充非法 base64 单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：补齐缓存图片 MIME/扩展名识别，`detect_mime_by_bytes()` 支持 AVIF、HEIC、HEIF、SVG，并将 WebP 的 RIFF 判断收紧到 `WEBP` 标记；`ext_from_mime()` 和 `guess_image_content_type()` 同步支持现代图片格式，避免缓存文件和 Web 预览误标成 PNG。补充 MIME 检测单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。
