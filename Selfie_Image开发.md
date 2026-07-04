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

2026-07-04：收紧用户参考图 HTTP 下载校验，`fetch_image_source()` 会拒绝非图片 Content-Type，二进制类型或缺失类型时使用图片签名确认，并和 provider 下载逻辑一样容忍非法 `Content-Length`。避免错误 JSON/文本响应被当作 PNG 参考图写入后续生成链路。补充 HTTP 参考图下载单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：统一 provider 响应图片签名判断，`providers.looks_like_binary_image()` 复用 `utils.looks_like_image_bytes()`，避免任意 RIFF 内容被误判为图片，同时支持 HEIF 和 XML 声明形式的 SVG。补充 provider 签名判断单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧所有 HTTP 图片下载后的内容校验，provider 结果图和用户参考图即使服务端声明 `image/*`，也必须通过图片签名检查，避免 JSON/文本错误响应被当作图片进入缓存或生成链路；同时补充 TIFF MIME/扩展名和签名识别。补充伪造 `image/png` 响应拒绝和 TIFF 单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧参考图 base64 内容校验，`data_url_to_bytes()` 在解码成功后也必须确认字节像图片，避免合法 base64 的 JSON/文本被 Web 形象上传或渠道测试误当 PNG 参考图。补充合法 base64 非图片 payload 拒绝单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强参考图 URL MIME 推断，`guess_image_content_type()` 会忽略 query/fragment 后再按扩展名判断，避免 `a.png?token=...`、`a.svg#icon` 等 URL 退回默认类型；同步将 `.svg` 纳入图片 URL 扩展名集合，并补充 `.jfif` 显式 JPEG 判断。补充带 query/fragment 的 MIME 推断单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧文本参考图 URL 识别，`looks_like_image_url()` 改为按 URL path 的真实图片后缀判断，避免 query 参数里的 `.png` 等扩展名导致非图片页面被误收，同时保留 QQ 图片域名和 `/download?` 入口兼容；Web 渠道测试和自拍形象上传的文件选择器同步放开 BMP、AVIF、HEIC、HEIF、TIFF、SVG。补充 URL 识别和 Web accept 列表单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：修正参考图 MIME 信任顺序，`data_url_to_bytes()` 和 `fetch_image_source()` 对已通过签名校验的图片统一返回字节检测出的 MIME，避免 data URL 或 HTTP 头声明错误导致后续上传、缓存和自拍形象保存使用错误 Content-Type；Web 自拍参考图保存也优先使用解析后的 MIME。provider 相对图片文件名判断复用统一扩展名集合，支持 `.tiff`、`.jfif`、`.svg` 等无斜杠相对路径。补充声明 MIME 不一致和现代相对文件名单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：补齐生成记录滚动淘汰时的缓存清理，`_record_task()` 在记录超过 100 条并裁剪旧记录时，会删除仅被淘汰记录引用的请求图/生成图缓存；新增 `collect_unreferenced_record_cache_paths()`，确保仍被保留监控记录引用的共享缓存文件不会被误删。补充共享路径保护单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：补齐 Web 记录清空接口的请求体校验，`POST /api/records/clear` 复用统一 JSON 对象检查，拒绝数组等非对象请求体，同时保持空对象 `{}` 清空记录的前端调用兼容。扩展 Web API 轻量单测覆盖鉴权、成功返回和非对象拒绝。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：继续补齐 Web 写接口请求体一致性，`POST /api/selfie-reference/clear` 和 `POST /api/selfie-profile/refresh` 也复用统一 JSON 对象检查，拒绝数组等非对象请求体；保持 Web 前端发送 `{}` 的清除参考图和刷新今日自拍设定调用兼容。扩展 Web API 轻量单测覆盖非对象拒绝和空对象成功路径。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧 Web JSON 解析边界，统一请求体读取在遇到非空但无法解析为 JSON 对象的 body 时返回 400，避免畸形 JSON 被 `silent=True` 当成空对象继续执行；空 body 仍按 `{}` 处理，保持清除/刷新类接口兼容。补充畸形 JSON 拒绝和空 body 成功单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider HTTP 错误预览提取，`http_error_preview()` 支持递归识别 `error_description`、`msg`、嵌套 `detail.message`、`errors[].message` 等常见代理错误结构，避免渠道测试或生成失败时只显示整段 JSON。补充多种错误结构单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 Web 刷新模型缓存的响应解析，`extract_model_ids_from_response()` 支持 `slug` 模型字段以及 `modelIds`、`model_ids`、`available_models`、`availableModels` 等容器字段，适配更多 OpenAI 兼容代理和模型聚合服务的列表格式。扩展模型 ID 提取单测。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider 通用图片响应解析，`collect_images_from_unknown()` 额外识别 `artifact/artifacts`、`asset/assets`、`file/files`、`href/src`、`downloadUrl/download_url`、`mediaUrl/media_url` 等常见资源字段，适配图片代理把生成图放在嵌套资源对象里的返回格式；相对路径仍按请求 base URL 解析并继续走图片签名校验。补充嵌套 `artifact.asset.downloadUrl` 单测。本轮不新增配置项，不影响 `_conf_schema.json`、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧 Web 后台生图任务查询接口，`GET /api/test-image-channel/tasks/<task_id>` 在鉴权后先校验任务 ID 必须符合插件生成的 `web-数字-数字` 格式，非法 ID 直接返回 400，合法但已清理/不存在的任务继续返回 404。补充无鉴权、非法 ID、成功查询和不存在任务的 Web API 单测。本轮不新增配置项，不影响 provider 请求/响应解析、图片缓存、生成记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧 Web 缓存图片读取路径校验，`_cache_absolute_path()` 会拒绝空路径和指向缓存目录本身的 `.`，`GET /api/cache-image` 缺失或空 `path` 现在返回 400 而不是落到“图片已清理”404；路径穿越仍保持 400。扩展缓存图片 Web API 单测覆盖缺失 `path`、空 `path` 和 `.`。本轮不新增配置项，不影响 provider 请求/响应解析、生成记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider base64 图片解析兼容性，`b64_to_bytes()` 支持 `base64://` 前缀、URL-safe base64 字符和缺失 padding 的返回格式；直接传入 `fetch_generated_image_url()` 的 data URL 也会经过图片签名校验，避免伪造 `data:image/...` 的 JSON/文本被接受。补充 URL-safe 无 padding base64 图片解析和伪 data URL 拒绝单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：将 base64 解码兼容逻辑下沉到共享 `decode_base64_payload()`，`data_url_to_bytes()` 和 provider `b64_to_bytes()` 统一支持 `base64://`、URL-safe base64 和缺失 padding；Web 自拍形象上传、渠道测试参考图和 provider 响应解析继续共享图片签名校验，避免伪图片进入链路。补充参考图 data URL / `base64://` / 纯 base64 的 URL-safe 无 padding 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider 通用 base64 响应字段兼容，`collect_images_from_unknown()` 额外识别 `image_base64/imageBase64`、`base64_image/base64Image`、`image_data/imageData`、`encoded_image/encodedImage` 等常见代理字段；仍只在内容通过图片签名校验后返回，重复图片会继续去重。补充 base64 字段别名单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 data URL 输入兼容，`data_url_to_bytes()`、命令/文本图片提取和 provider 文本响应解析都支持 `data:image/...;name=...;charset=...;base64,...` 这类带额外参数的 data URL；解码后仍继续走图片签名校验。补充工具层和 provider 层参数化 data URL 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：增强 provider Markdown 图片链接兼容，`clean_image_url()` 会剥离 Markdown 目标里的可选标题参数，支持 `![result](outputs/a.png "preview")` 和 `![result](<outputs/a.webp> 'preview')` 这类相对结果图链接继续按 provider base URL 解析下载；图片内容仍走现有 HTTP Content-Type、大小和签名校验。补充 provider 文本提取和相对 Markdown 链接下载单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-04：收紧本地参考图文件读取校验，`fetch_image_source()` 的本地文件路径分支现在也必须通过图片签名检查，避免合法存在但内容为 JSON/文本的小文件被 `detect_mime_by_bytes()` 默认当作 PNG 进入后续生成链路；有效本地图片仍按字节检测出的 MIME 返回。补充本地非图片文件拒绝和有效图片读取单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、provider 响应解析、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：增强内联图片 base64 输入兼容，参考图解析、文本图片提取和 provider 响应解析统一支持大小写混用的 `data:image/...;base64,...` 与 `base64://...` 前缀；provider 文本响应也可直接提取 `base64://...`。解码后仍继续走图片签名校验。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 通用响应解析，`collect_images_from_unknown()` 可识别响应数组或嵌套字段里的裸 base64 字符串，适配部分 OpenAI 兼容代理直接返回 `image: "iVBOR..."` 或 `data: ["iVBOR..."]` 的格式；仍需通过图片签名校验后才进入生成结果。补充裸 base64 字符串单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：优化图片缓存上限清理顺序，`_cleanup_image_cache_if_needed()` 会优先删除未被当前监控记录引用的旧缓存图，再删除仍被记录引用的旧图，并继续保护本次请求图/生成图；新增 `collect_cache_cleanup_candidates()` 和单测，避免缓存超限时过早清掉最近 100 条记录仍在展示的图片。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、provider 请求/响应解析或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：收紧错误预览敏感信息处理，新增 `redact_sensitive_text()`，provider HTTP 错误预览、审核接口失败和 Web 刷新模型列表错误会遮蔽常见 `Authorization: Bearer ...`、`api_key=...`、JSON `token/api_key/secret`、`sk-...`、`AIza...` 等密钥形态，避免渠道监控或测试错误信息带出真实凭据。补充错误预览脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、缓存记录或用户额度。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续收紧错误脱敏边界，fallback 生成器会遮蔽 adapter 返回错误、异常和成功结果附带的 attempts，Web API 统一错误响应和后台生图任务状态也会脱敏；provider 非 JSON 响应和通用返回预览改用 `response_preview()`，避免代理把请求头、token 或 key 混进预览时进入监控记录或前端。补充 fallback 错误/异常脱敏、provider 预览脱敏和 Web API 错误响应脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、图片缓存清理、生成记录结构或用户额度；Web API 只改变错误文本的敏感片段展示。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强监控和 Web 边界安全，`redact_sensitive_text()` 额外遮蔽 URL basic auth、proxy/password、Cookie、`x-api-key`、`x-goog-api-key` 等形态，`redact_sensitive_data()` 会按结构化 key 整值遮蔽更多 header/secret 字段，`response_preview()` 对结构化对象先做数据级脱敏；Web 后台任务查询和缓存图片读取新增长度上限，刷新模型列表错误数组也只保存脱敏后的 URL 和异常文本。补充代理 URL 凭据、结构化 header、超长 task_id 和超长 cache-image path 单测。本轮不新增配置项，不影响 `_conf_schema.json`、provider 请求构造、图片缓存清理、生成记录结构或用户额度；Web API 只新增异常输入的 400 拒绝。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：补齐历史监控记录和后台任务查询的兜底脱敏，`_load_records()`、`_persist_records()`、`get_recent_records()` 和 `get_web_image_task()` 都会在读写/返回前调用 `redact_sensitive_data()`，Web `/api/records` 和 `/api/test-image-channel/tasks/<task_id>` 也在路由层再次脱敏，避免旧版本已落盘记录或旧任务状态把密钥带到前端。补充 Web records/task status 路由脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、provider 请求/响应解析、图片缓存清理或用户额度；历史记录再次保存后会持久化脱敏结果。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：收紧 Web API 缓存和 Token 鉴权边界，所有 `/api/` 响应统一附加 `Cache-Control: no-store`、`Pragma: no-cache`、`Expires: 0`，避免配置、监控记录或任务状态被浏览器/代理缓存；所有 Web 响应附加 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`；Token 校验会拒绝空值、超长值和非 ASCII 异常输入，避免 `hmac.compare_digest()` 抛出 500，同时支持多个 Token 头中任一有效值通过，避免错误 Authorization 头阻断正确的 `X-Selfie-Image-Token`。补充 API no-store、安全响应头、非 ASCII Token、超长 Token 和多 Token 头单测。本轮不新增配置项，不影响 `_conf_schema.json`、provider 请求/响应解析、图片缓存清理、生成记录结构或用户额度；Web API 只改变响应头和异常 Token 的拒绝方式。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：补齐 Web Token 默认弱口令边界，`check_auth()` 在对外监听地址上会拒绝 `changeme`、`password`、`admin`、`123456`、`test` 等占位 Token，即使请求头匹配也返回 401；本机监听仍允许这些占位值用于本地调试，空 Token 的本机免校验规则保持不变。补充对外监听弱 Token 拒绝和本机弱 Token 可用单测。本轮不新增配置项，不影响 `_conf_schema.json`、provider 请求/响应解析、图片缓存清理、生成记录结构或用户额度；Web API 只改变弱 Token 在外部监听时的鉴权结果。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续补 Web 鉴权和 provider 响应解析兼容，Token 比较改为 UTF-8 bytes `hmac.compare_digest()`，避免非 ASCII 配置 Token 触发异常并支持用户配置非 ASCII Token；provider 通用响应解析新增 `uri/imageUri/image_uri/resource/resources/resource_url/resourceUrl/public_url/publicUrl` 等资源字段识别，可从嵌套资源对象里解析相对图片路径并按 provider base URL 下载。补充非 ASCII 配置 Token 和资源 URL 别名单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API 路由、图片缓存清理、生成记录结构或用户额度；provider 响应解析兼容面扩大但仍需图片签名校验。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 通用响应解析，`collect_images_from_unknown()` 会尝试解析文本字段里的 JSON 或 fenced JSON，例如 OpenAI 兼容代理把 `{"image":{"uri":"/outputs/a.png"}}` 放在 `message.content` 时也能继续提取相对图片路径并按 provider base URL 下载；普通文本、Markdown 图片链接和裸 base64 解析路径保持不变，结果仍需图片签名校验。补充 fenced JSON 文本内容解析单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 文本响应解析，除整段 JSON/fenced JSON 外，也支持普通文本中嵌入 fenced JSON，以及 SSE 风格的 `data: {...}` / `data:{...}` 行；重复 JSON payload 会去重后再递归解析，避免相同图片 URL 被重复下载。补充嵌入式 fenced JSON、SSE data JSON 和紧凑 SSE data JSON 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面，结果仍需图片签名校验。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续收紧敏感信息脱敏，`redact_sensitive_text()` 和 `redact_sensitive_data()` 会识别 `accessToken`、`clientSecret`、`refreshToken` 等 camelCase 密钥字段，避免 provider 错误预览、结构化响应预览或旧监控记录里漏出常见前端/代理字段名。补充 raw 文本和结构化 response preview 脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、provider 请求/响应解析、图片缓存清理、生成记录结构或用户额度；只扩大脱敏覆盖面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续补齐 raw 文本敏感信息脱敏，`redact_sensitive_text()` 支持 `access_token=...`、`x-api-key: ...`、`client_secret=...` 等非 JSON 键值形态，避免代理错误消息或模型列表刷新异常把常见密钥字段以纯文本形式带入 Web 错误响应或监控记录。补充 raw text 脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、provider 请求/响应解析、图片缓存清理、生成记录结构或用户额度；只扩大脱敏覆盖面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 通用响应解析，字符串节点如果本身是相对图片路径或现代图片文件名（如 `outputs/a.png`、`generated.webp`）也会进入相对 URL 下载候选，适配代理返回 `{"images":["outputs/a.png"]}` 这类数组格式；仍按 provider base URL 解析并走图片签名校验。补充普通字符串数组相对路径单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 通用资源字段兼容，`collect_images_from_unknown()` 新增识别 `path/filePath/file_path/imagePath/image_path/outputUrl/output_url/resultUrl/result_url` 等常见图片路径字段，可从 OpenAI 兼容代理或文件代理返回的路径字段里解析相对图片 URL；仍按 provider base URL 下载并走图片签名校验。补充 path/filePath/outputUrl/resultUrl 字段单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：补齐 fallback 生成器最终失败和全局超时出口的脱敏边界，返回 `error` 和 `attempts` 前统一遮蔽敏感字段；同时对使用中的模型标签做脱敏，避免渠道名或模型名里误带 `api_key/token` 等片段时进入错误结果或监控记录。补充最终失败 attempts 脱敏单测。本轮不新增配置项，不影响 `_conf_schema.json`、provider 请求/响应解析、图片缓存清理或用户额度；只收紧错误结果展示。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 通用资源字段兼容，`collect_images_from_unknown()` 新增识别 `link/location/signedUrl/signed_url/cdnUrl/cdn_url` 等常见图片链接字段，适配文件代理、CDN 代理或签名 URL 聚合服务返回的嵌套资源格式；相对路径仍按 provider base URL 解析下载并继续走图片签名校验。同时主生成链路返回的 `request_data.targets` 会先脱敏，避免渠道名或模型名误带敏感片段时通过 Web 测试响应直接展示。补充链接字段别名单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API 路由、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面并收紧返回数据展示。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 文本响应解析，`extract_image_urls_from_text()` 支持无引号 HTML 图片标签 `src`，例如 `<img src=/outputs/a.png>` 或 `<img src=outputs/a.webp>`，可继续按 provider base URL 解析相对结果图；下载后仍经过 Content-Type、大小和图片签名校验。补充无引号 HTML `img src` 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider HTML 文本响应解析，`extract_image_urls_from_text()` 支持 `<img srcset="...">` 和 `<source srcset="...">`，可从 `srcset` 候选里提取相对图片路径或绝对图片 URL，例如 `/outputs/a.webp 1x, /outputs/a@2x.webp 2x`；相对路径仍按 provider base URL 解析下载，下载后继续走 Content-Type、大小和图片签名校验。补充 HTML `srcset` 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider HTML 文本响应解析，`extract_image_urls_from_text()` 支持 `<a href="...">` 和 `<link href=...>` 里的图片链接，可从下载链接或 preload link 中提取相对结果图；普通锚点如 `#top` 不会进入下载候选。相对路径仍按 provider base URL 解析下载，下载后继续走 Content-Type、大小和图片签名校验。补充 HTML `href` 图片链接单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider HTML/CSS 文本响应解析，`extract_image_urls_from_text()` 支持 CSS `url(...)` 里的图片候选，可从 `background-image:url('/outputs/a.png')` 或 `url(outputs/a.webp)` 这类返回中提取相对结果图；普通锚点或非图片候选仍不会进入下载列表。相对路径继续按 provider base URL 解析下载，下载后走 Content-Type、大小和图片签名校验。补充 CSS `url(...)` 图片链接单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider HTML 文本响应解析，`extract_image_urls_from_text()` 支持懒加载图片属性 `data-src/data-original/data-lazy-src/data-url`，可从代理页面或文件页里提取相对结果图；非图片候选仍会被过滤。相对路径继续按 provider base URL 解析下载，下载后走 Content-Type、大小和图片签名校验。补充懒加载 HTML 图片属性单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider HTML 文本响应解析，`extract_image_urls_from_text()` 支持懒加载 `srcset` 属性 `data-srcset/data-lazy-srcset/data-original-srcset`，可从多尺寸候选里提取相对结果图或绝对图片 URL；非图片候选仍会被过滤。相对路径继续按 provider base URL 解析下载，下载后走 Content-Type、大小和图片签名校验。补充懒加载 HTML `srcset` 属性单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只扩大 provider 响应解析兼容面。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：修正 provider 响应图片 URL 解析，`resolve_response_url()` 支持协议相对 URL（如 `//cdn.example.test/outputs/a.png`），会沿用 provider base URL 的 `http/https` scheme，而不是误拼成 base URL 下的相对路径。补充协议相对图片 URL 下载单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只修正 provider 响应 URL 解析兼容性。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 文本响应 URL 清理，`clean_image_url()` 会剥离裸图片 URL 末尾常见 ASCII 标点 `,`、`.`、`;`，避免 `result https://.../a.png,` 或 `.../a.webp.` 这类自然语言返回把标点带进下载地址；中文标点清理保持不变。补充裸 URL 尾随 ASCII 标点单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只提升 provider 文本响应解析容错。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。

2026-07-05：继续增强 provider 文本响应 URL 提取，`extract_image_urls_from_text()` 会兼容 JSON 转义斜杠形式的裸 URL，例如 `https:\/\/example.test\/outputs\/a.png`，避免代理把 URL 作为 JSON 字符串片段输出时无法下载；尾随标点清理继续生效。补充 JSON 转义斜杠 URL 单测。本轮不新增配置项，不影响 `_conf_schema.json`、Web API、图片缓存清理、生成记录结构或用户额度；只提升 provider 文本响应解析容错。验证命令：`python -m unittest tests/test_core.py`、`python -m py_compile __init__.py constants.py generator.py main.py models.py persona.py preset.py providers.py utils.py web.py`。
