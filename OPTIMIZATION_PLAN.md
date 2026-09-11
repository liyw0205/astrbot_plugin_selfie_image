# Selfie Image 优化与新功能计划

> 本计划以仓库当前代码为事实来源，不把愿望清单写成既有能力。
> 当前基线：插件 `1.6.14`，提交 `8ba3353`（`fix cache cleanup ordering by generation record time`）。
> 任何条目进入开发前，都必须先补充真实复现、用户反馈或运行数据；没有证据的条目保持 `idea`。

## 1. 当前实现边界

以下能力已经存在，后续计划只做逻辑收敛、可靠性改进或在明确缺口上扩展：

| 子系统 | 现有行为 | 代码入口与数据 |
| --- | --- | --- |
| 聊天入口 | `/画`、`/文生图`、`/图生图`、`/自拍`、`/合影`、`/视频`、形象/预设/任务命令；LLM 工具 `generate_image`、`generate_selfie`、`generate_video`、`retry_last_generation` | `main.py` 命令处理器与 LLM tool；`prompts/` |
| 图片生成 | 多协议、多渠道、多模型、顺序/随机/固定调用、失败回退、API Key 轮换、渠道冷却 | `core/models.py`、`core/providers.py`、`generation/generator.py`、`core/error_classify.py` |
| 视频生成 | 文生、图生、形象视频；多协议轮询、时长/比例校验、分镜解析 | `generation/video.py`、`main.py`、`features/creative_features.py` |
| 任务生命周期 | Web 与聊天任务共用 `generation_tasks.json`；去重、排队/运行/终态、取消、重试、删除、导出、重启后 `expired` 对账、每日额度预占与释放 | `tasks/task_manager.py`、`main.py` |
| 记录与资产 | SQLite 生成记录；旧 JSON 迁移保留 `.bak`；媒体来源 sidecar；记录复用、收藏/置顶/标签/备注、分页、比较、元数据导入导出 | `generation/record_database.py`、`generation/generation_store.py` |
| 缓存 | 图片和视频共用 `image_cache/`（视频在 `video/`）；按数量/大小清理，保护记录引用和收藏/置顶媒体；Web 清理有预览 token | `generation/generation_store.py`、`core/utils.py` |
| 形象与参考图 | 主形象、最多 3 张辅助形象、形象类型；消息/引用/转发图片分桶、去重和机器人头像过滤 | `features/persona.py`、`features/reference_collector.py`、`features/reference_media.py` |
| 上下文 | 最近消息和图片用于后续请求；每会话最多 40 条、最多 100 个会话；最近一次 LLM 生图参数保存在内存 | `features/conversation_context.py`、`main.py` |
| 创作与画布 | 提示词模板/变体、视频 storyboard、COS 内置/自定义池；画布会话持久化，最多 40 会话、12 槽位、24 个结果 | `features/creative_features.py`、`cos/`、`studio/studio.py`、`studio/studio_adapter.py` |
| 管理端 | 独立 Flask `/api/*` 与 AstrBot Dashboard 页面 API；二者共用 `webui/services.py` 的查询、健康和错误数据契约 | `webui/web.py`、`webui/dashboard_api.py`、`webui/services.py`、`webui/contracts.py` |
| 配置 | 启动配置只含 Web 开关/地址/端口/Token；业务配置保存到 `selfie_image_config.json`，schema version 为 2，保存前做渠道预检和敏感字段恢复 | `_conf_schema.json`、`core/models.py`、`features/config_manager.py` |

## 2. 计划规则

### 2.1 目标状态

状态使用：`idea -> discovered -> ready -> in_progress -> verify -> done`。因外部环境或需求未确认而暂停使用 `blocked`，被替代的条目标记 `closed`。

优先级：

- `P0`：会导致重复扣额度、数据丢失、凭据泄露或任务无法判断终态。
- `P1`：影响生成成功率、重试成本、任务可追踪性或主要操作流程。
- `P2`：体验、性能、可观测性和维护性改进。

范围标签：`BE`（Python/任务/渠道）、`FE`（Dashboard/浏览器）、`DATA`（配置/SQLite/JSON/缓存）、`E2E`（跨层链路）。

### 2.2 每个 Goal 必须回答

1. 触发条件和真实用户是谁？现行为与期望行为是什么？
2. 使用哪条最小输入可以稳定复现？代码入口和持久化对象是什么？
3. 成功指标如何测量？失败、取消、刷新、重启和重复提交怎么验收？
4. 是否改变配置、任务字段、记录 schema、缓存文件或 API 响应？
5. 失败如何回滚，旧数据和旧入口如何继续读取？

## 3. 旧功能逻辑优化

这些条目都对应现有代码路径；它们不是已确认缺陷，进入 `ready` 前必须补证据。

### OPT-001 统一聊天任务与 Web 任务的状态语义

- **优先级/范围**：P1，BE / E2E
- **事实依据**：`main.py:start_command_image_task` 和 `tasks/task_manager.py:start_web_image_task` 都写入 `_web_tasks`；终态同时包含生成结果、发送状态、取消请求、重试信息和 `record_ids`。
- **要优化**：建立一份状态矩阵，核对图片、视频、画布、批量任务在 `queued/running/partial_success/succeeded/failed/cancelled/expired` 下的字段、额度释放、记录落库和通知状态；发现分支差异后收敛到共享函数。
- **验收**：同一任务从创建到终态只释放一次额度；取消与 provider 完成竞态不覆盖成功证据；进程重启后生成任务、记录和通知状态可互相定位；四类入口各有回归测试。
- **不做**：不改变现有命令名、任务保留策略或 provider 协议。

### OPT-002 收敛渠道失败分类、回退与冷却策略

- **优先级/范围**：P1，BE / DATA
- **事实依据**：`generation/generator.py`、`generation/video.py` 产生 attempts；`core/error_classify.py` 判断重试/换 Key/换模型；`generation/generation_store.py` 在内存中维护渠道健康和冷却。
- **要优化**：逐项验证图片、视频、审核、图转文四类调用是否使用同一错误类别和脱敏规则；补齐“跳过冷却渠道、所有渠道冷却、最后一次失败”的用户可见原因与记录字段。
- **验收**：一次请求的尝试顺序、跳过原因、重试次数与实际 HTTP 调用一致；不可重试错误不再盲目回退；健康接口不泄露 Key；固定的 fake provider 矩阵覆盖超时、401、429、5xx、空响应和下载失败。
- **不做**：不凭空增加重试次数、并发或渠道；不让诊断接口自动改路由。

### OPT-003 完善配置保存、迁移与预检的闭环

- **优先级/范围**：P1，BE / DATA / E2E
- **事实依据**：`AICatConfig.from_dict` 执行 schema 迁移、旧 key 兼容、proxy 迁移和渠道规范化；`features/config_manager.py` 负责导入预览、敏感字段恢复和保存前 `preflight_config_channels`；健康接口已返回 `config_preflight`。
- **要优化**：为配置导入、部分渠道更新、空模型自动禁用、masked secret 回填建立 round-trip 测试，并区分“未配置、无效、不可用、预检异常”四种状态。
- **验收**：导出再导入不丢未知兼容字段和代理密码；预检失败不覆盖旧配置；独立 Flask 与 Dashboard 返回一致的诊断；升级旧 schema 有备份且可回读。
- **不做**：不把 API Key、Token、代理密码写入日志、前端源码或测试快照。

### OPT-004 记录、sidecar 与缓存的一致性修复工具

- **优先级/范围**：P1，DATA / BE
- **事实依据**：记录主体在 `generation_records.sqlite3`，媒体来源在 `media_sources/*.json`，缓存由 `_cache_cleanup_plan` 按记录引用、收藏/置顶和记录时间保护；启动时会清理孤儿媒体。
- **要优化**：增加只读一致性检查，报告记录缺失缓存、孤儿 sidecar、sidecar 缺失、不可解析路径和记录引用的重复文件；对可修复项提供明确的 dry-run 和逐项结果。
- **验收**：检查不删除文件、不修改记录；修复前后可生成报告；清理永远不删除仍被记录/资产引用的媒体；旧 JSON 迁移、共享缓存、视频文件和异常中断均有临时目录测试。
- **不做**：不默认扩大缓存上限，不执行无确认的批量删除，不用文件 mtime 覆盖记录时间排序。

### OPT-005 双 Web 入口的契约与页面状态回归

- **优先级/范围**：P1，FE / BE / E2E
- **事实依据**：`webui/web.py` 和 `webui/dashboard_api.py` 暴露相同业务入口；查询解析、分页边界、健康 payload、脱敏和错误 envelope 由 `webui/services.py` 共享；`pages/dashboard/index.html` 是源文件，`webui/index_fallback.py` 是生成产物。
- **要优化**：把新增/修改接口的请求参数、状态码、成功/失败 envelope 和鉴权边界写成矩阵测试；对任务轮询、重复点击、刷新、空列表、401/404/409/500 补浏览器实测。
- **验收**：两个入口对同一输入返回等价业务字段；页面源与 fallback 校验通过；一次提交只创建一个任务；媒体加载使用权限校验和缓存去重；移动端无内容遮挡。
- **不做**：不删除 Flask、Dashboard 或兼容路由，不直接编辑 fallback 产物。

### OPT-006 参考图、上下文和自拍路由的可解释性

- **优先级/范围**：P1，BE / E2E
- **事实依据**：`ReferenceCollector` 对用户图、引用/转发图、机器人头像分桶去重；`ConversationContextMixin` 在内存中保留最近消息和图片；`main.py` 根据普通生图、图生图、自拍、合影和后续编辑选择不同参考图。
- **要优化**：为每类入口记录脱敏的选择结果（来源角色、数量、是否使用主/辅助形象、是否回退上下文），并检查“普通生图不静默带形象图”“合影只用主形象”“明确引用优先于历史图片”这些边界。
- **验收**：同一消息在用户图、机器人图、回复图、无图四种输入下得到稳定的参考图集合；请求记录只保存数量/角色/摘要，不保存不必要的原始凭据；去重和最大数量限制有测试。
- **不做**：不把所有历史图片默认带入生成，不改变现有权限和审核策略。

### OPT-007 审核、翻译和辅助模型的失败策略

- **优先级/范围**：P1，BE / DATA
- **事实依据**：`features/audit_pipeline.py` 支持提示词审核、出图审核、OCR/图转文和中英文提示词转换，可选专用渠道或 AstrBot 当前 LLM；翻译失败按当前代码保留原文。
- **要优化**：统一每个辅助调用的超时、空响应、模型不存在、解析失败和 fail-open/fail-closed 语义；把实际使用的辅助模型与跳过原因写入脱敏请求摘要。
- **验收**：审核开启但未配置模型时行为明确且可见；审核拒绝不会产生可发送结果；翻译失败不会丢失原始提示词；图片读取失败不会误报审核通过；覆盖图片、视频和无事件 Web 试画。
- **不做**：不在没有产品规则的情况下放宽审核，不把审核提示词或图片内容写入普通日志。

## 4. 新功能候选

以下是从现有模型和入口自然延伸出的功能，不代表已经承诺开发。每项先收集需求证据，再拆成独立 Goal。

### FEAT-001 画布会话复制与模板化

- **现有基础**：`StudioStore` 已持久化会话、槽位、graph、最近结果和 `last_run`；当前有创建、更新、删除、槽位排序、结果提升和运行 API。
- **最小版本**：增加“复制会话”，只复制标题、模板、graph 和有效槽位，清除运行状态与结果引用；可选保存为用户模板。
- **约束**：不复制正在运行的 task；缓存路径必须重新校验；最多 40 个会话、12 个槽位的现有限制继续生效。
- **验收**：复制后修改任一会话不影响源会话；重启后两者都可加载；无效/已删除媒体给出单项错误，不使整个会话不可用。

### FEAT-002 聊天端复用历史记录

- **现有基础**：`generation_store.py:get_record_reuse_payload` 和 Web `records/<record_id>/reuse` 已能返回复用参数；记录详情带有任务摘要和脱敏字段。
- **最小版本**：在聊天端增加一个明确的“按记录编号重新生成”入口，复用提示词、比例、分辨率、参考图和可选模型，并创建新的任务/记录链路。
- **约束**：默认不复制旧任务 ID、不绕过权限/额度/审核；缺失缓存时必须在创建任务前失败。
- **验收**：新旧记录可通过 `source_record_id` 或等价字段关联；重复提交可去重；图片与视频类型不能混用；失败可以独立重试和删除。

### FEAT-003 可恢复的会话上下文管理

- **现有基础**：`ConversationContextMixin` 目前仅在内存保存最近 40 条消息、100 个会话，以及最近一次 LLM 生图参数，进程重启后自然丢失。
- **最小版本**：增加显式的上下文查看/清除和可选持久化开关；默认仍保持当前内存行为，持久化内容只保存截断文本、角色、时间和媒体摘要。
- **约束**：不默认持久化原始图片或敏感消息；按会话权限隔离；达到上限时按现有 LRU 规则淘汰。
- **验收**：关闭开关时无新增文件；开启后重启可恢复且超限可淘汰；清除只影响当前会话；上下文引用不改变普通生图不带形象图的规则。

### FEAT-004 渠道健康历史与可导出诊断

- **现有基础**：`GenerationStoreMixin` 已记录渠道 attempts、成功率、平均耗时、连续失败和冷却状态；`/api/health`、Dashboard health 可读取当前内存快照，且可清除。
- **最小版本**：提供时间窗口聚合和脱敏导出，区分图片、视频、审核/辅助调用，并保留清除当前快照的现有接口。
- **约束**：不保存 API Key、完整 URL 查询串、提示词或图片；不让导出数据改变回退策略。
- **验收**：重启前后持久化策略明确；聚合数字与生成记录 attempts 对得上；没有调用记录时显示空状态而非虚假的成功率。

### FEAT-005 资产集合与批量工作流

- **现有基础**：资产已有 favorite、pinned、tags、note、分页、批量元数据、删除、导入导出和批量加入画布。
- **最小版本**：在不破坏现有字段的前提下增加命名集合，支持从资产页批量加入集合并将集合一次性送入画布。
- **前置验证**：先确认标签/收藏无法满足真实使用场景，再设计 SQLite schema、导入导出版本和权限边界。
- **验收**：集合删除不删除媒体；集合中的缺失记录可诊断；导入冲突可预览；批量操作具有上限、取消和部分失败结果。

## 5. 交付与验证门

### G0：发现

- 记录用户、触发条件、最小复现、代码入口、数据对象和“不做项”。
- 对新功能先确认调用方：聊天命令、LLM tool、Flask、Dashboard 还是内部 API；没有调用方不进入 `ready`。

### G1：本地自动化

```bash
PYTHONPATH=.. pytest -q
python -m compileall -q core features generation tasks webui prompts studio cos main.py
python scripts/check_dashboard_fallback.py
git diff --check
```

修改 `pages/dashboard/index.html` 时：

```bash
python scripts/generate_dashboard_fallback.py
python scripts/check_dashboard_fallback.py
```

涉及任务状态、共享 Web API、配置/存储字段或 provider 回退时，必须增加对应回归测试，不能只测成功路径。

### G2：浏览器验证

- 独立面板默认入口：`http://127.0.0.1:14514/`；先请求 `/api/health`，确认 `config_preflight` 和 Token 状态。
- 至少覆盖手机竖屏、正常提交、重复点击、刷新恢复、任务取消、失败/重试、空列表、401/409/500、长提示词和媒体加载。
- 记录设备、浏览器、时间、URL、步骤、预期/实际结果；截图放在 `/tmp`，不进 Git。

### G3：后端验证

- 使用测试渠道和可清理数据；真实 provider smoke test 固定请求数为 1、明确超时和任务 ID。
- 观察完整链路：请求校验 -> 任务快照 -> provider attempts -> 记录/缓存 -> 发送状态 -> 终态查询。
- 日志和导出结果必须脱敏；不得在计划或仓库保存 Token、Key、Cookie、SSH 信息。

### G4：发布与回滚

- 配置、SQLite、JSON、sidecar 或缓存有变化时，先备份整个插件数据目录。
- 新字段提供默认值，旧数据缺字段不能导致列表为空；迁移失败保留旧文件。
- 代码回滚与数据恢复分开验证；任何删除/清理/批量导入先有预览或确认。
- `done` 只在自动化、需要的浏览器/后端实测和回滚步骤都有证据后使用。

### 本轮实现与证据（2026-09-12）

以下条目已完成代码实现，并通过本地自动化、可控测试渠道和 Web 运行验证，进入 `done`：

- `OPT-001`：补充共享终态归一化矩阵，覆盖图片/视频、成功、部分成功、投递失败、失败、取消和晚到成功；任务快照保留 `record_ids`、通知状态与幂等额度释放证据。
- `OPT-002`：图片/视频 fake provider 回退测试覆盖鉴权失败、服务异常、参数/空响应、冷却跳过、全冷却最早恢复和超时不重提；视频鉴权失败现可切换备用渠道，超时仍停止跨渠道重提。
- `OPT-003`：配置导出/导入预览与应用覆盖脱敏 API key、多 API key、代理密码、未知兼容字段和预检失败边界；遮罩凭据在 round-trip 中恢复而不出现在响应。
- `OPT-004`：新增只读记录/缓存/sidecar 一致性扫描和确认式孤儿文件修复；覆盖图片、视频、缺失媒体、重复引用和非法路径。
- `OPT-005`：新增 Flask 与 Dashboard 的上下文、健康历史、一致性、资产集合和画布复制路由；共享参数校验和响应矩阵测试已补齐。
- `OPT-006`：记录参考图来源角色、数量和上下文回退摘要；不写入原始来源凭据。
- `OPT-007`：审核、翻译和输出读取失败策略已明确，辅助调用失败均有脱敏结果或跳过原因。
- `FEAT-001`：画布会话复制，重映射槽位并清空运行结果，运行中会话拒绝复制。
- `FEAT-002`：聊天端 `/记录复用`（别名 `/记录重生`）复用历史记录请求并保留来源链路。
- `FEAT-003`：上下文查看/清除命令与 API，以及默认关闭的截断持久化开关。
- `FEAT-004`：渠道健康历史持久化、窗口/媒体类型聚合和脱敏导出。
- `FEAT-005`：资产集合 JSON 索引、导入预览、缺失记录诊断和批量送入画布。

本轮追加验收证据：

- 配置迁移：`schema_version=1` 与 legacy camelCase 配置在临时数据目录中升级为 v2；迁移前生成 `selfie_image_config.json.bak`，写入后重新回读；模拟写入失败时原文件字节保持不变。
- 任务执行器：Web 图片和视频 runner 实际执行晚到取消竞态，最终状态保持 `succeeded`、清除取消标记并确认投递；聊天 runner 实际执行终态发布并确认额度释放只调用一次。
- 双 Web 契约：Flask 与 Dashboard 对成功、401、404、409、500、501 响应矩阵均有测试；独立 Flask 页面用 Chromium headless 加载成功（DOM 约 1.18 MB），无 Token API 为 `401`，带 Token 健康 API 为 `200`；390×844 移动视口截图确认首屏布局无内容遮挡。
- 全量验证：`PYTHONPATH=.. pytest -q` 为 `459 passed, 7 subtests passed`；`compileall`、Dashboard fallback 生成/校验和 `git diff --check` 均通过。

本地证据：

```text
PYTHONPATH=.. pytest -q                 459 passed, 7 subtests passed
python -m compileall -q core features generation tasks webui prompts studio cos main.py
python scripts/generate_dashboard_fallback.py
python scripts/check_dashboard_fallback.py
git diff --check
```

额外边界验证确认 `/api/health/history` 在 Flask 与 Dashboard 均拒绝负数及超过 31 天的窗口；一致性扫描确认保留的视频路径不会被误报为孤儿。独立 Flask fake-plugin smoke test（`127.0.0.1:14519`）确认页面可由 Chromium headless 加载（DOM 约 1.18 MB，标题为 `Selfie Image 管理面板`）、无 Token API 返回 `401`、带 Token 记录 API 返回 `200`；迁移备份和移动视口证据见上方追加验收条目。

## 6. 当前看板

| ID | 类型 | 范围 | 优先级 | 状态 | 下一步 |
| --- | --- | --- | --- | --- | --- |
| `OPT-001` | 旧功能优化 | BE / E2E | P1 | done | 终态矩阵、四类 runner、重启对账和额度幂等均有回归证据 |
| `OPT-002` | 旧功能优化 | BE / DATA | P1 | done | 图片/视频/辅助调用的错误分类、回退、冷却和脱敏均有测试渠道证据 |
| `OPT-003` | 旧功能优化 | BE / DATA / E2E | P1 | done | 脱敏 round-trip、v1 迁移备份/回读/失败恢复和双 Web 诊断均已验证 |
| `OPT-004` | 旧功能优化 | DATA / BE | P1 | done | 图片/视频、sidecar、孤儿、非法路径和确认式修复均有临时目录证据 |
| `OPT-005` | 旧功能优化 | FE / BE / E2E | P1 | done | Flask/Dashboard 契约矩阵、重复请求去重、错误状态和桌面/移动首屏已验证 |
| `OPT-006` | 旧功能优化 | BE / E2E | P1 | done | 消息/引用/转发/上下文/主辅形象选择与脱敏摘要均有回归测试 |
| `OPT-007` | 旧功能优化 | BE / DATA | P1 | done | 审核、翻译、图转文和输出读取的超时/空响应/无模型/fail-open 边界已验证 |
| `FEAT-001` | 新功能 | FE / BE / DATA | P2 | done | 复制重映射、运行中冲突、结果清理、无效媒体反馈和重启持久化已验证 |
| `FEAT-002` | 新功能 | BE / E2E | P2 | done | 聊天记录复用的类型校验、来源链路、去重、额度和独立失败重试已验证 |
| `FEAT-003` | 新功能 | BE / DATA | P2 | done | 持久化开关、重启恢复、会话隔离、上限淘汰和清除边界已验证 |
| `FEAT-004` | 新功能 | BE / DATA / FE | P2 | done | 健康历史持久化、重启聚合、空数据、媒体筛选和脱敏导出已验证 |
| `FEAT-005` | 新功能 | FE / BE / DATA | P2 | done | 集合导入预览、冲突跳过、缺失诊断、批量上限和画布送入已验证 |

## 7. 明确不纳入计划的猜想

- 不把 README 中已经存在的命令、渠道、预设、任务中心、资产库或画布重新列为“新功能”。
- 不在没有运行数据时承诺扩大并发、缓存上限、任务保留量、批量上限或重试次数。
- 不默认把普通生图改成自拍，不默认把历史上下文或主形象图带入请求。
- 不删除 Flask、Dashboard、旧配置字段、旧 JSON 迁移入口或兼容协议，除非先有调用方搜索和迁移方案。
- 不用“代码已修改”作为完成标准；每个目标必须留下测试命令、复现输入、任务/记录标识和脱敏结果。
