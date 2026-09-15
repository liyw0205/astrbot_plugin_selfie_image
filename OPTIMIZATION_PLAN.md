# 后续优化计划

> 基线版本：`5547185`（2026-09-16）。本文只记录当前代码仍存在、且可以由代码直接证明的旧实现问题；没有运行数据或测试证据的内容标为“需先观测”，不把推测写成结论。

## 1. 当前基线

- 本地 `main` 与 `origin/main` 均指向 `5547185`；工作区的 `.tmp_douyin/`、`dy1.html`、`dy2.html` 是未跟踪临时文件，不属于本计划。
- `PYTHONPATH=.. pytest -q`：`488 passed, 12 subtests passed`。
- `python -m compileall -q core features generation tasks webui prompts studio cos main.py` 已通过。
- 生成记录优先使用 `generation_records.sqlite3`，`generation/generation_store.py:261-268, 604-654` 仍保留旧 `generation_records.json` 迁移和无数据库回退。
- Web 任务、画布会话、预设/COS/配置等仍有多个 JSON 存储；统一媒体缓存位于 `image_cache`（`main.py:271-279`）。
- 插件入口 `main.py` 当前约 7147 行，图片、视频、自拍、画布和 Web 命令的生命周期仍由同一入口及 mixin 协作完成。

## 2. 后续 backlog

每项都必须先做基线观测，再加回归测试，最后改实现。除非“不可改变的契约”明确允许，不得借优化之名改变现有命令、协议或数据语义。

### P0：Web 任务状态整表写入

**代码证据**

- `tasks/task_manager.py:155-158` 的 `_persist_web_tasks_locked()` 每次把完整 `self._web_tasks` 写入 `generation_tasks.json`。
- `tasks/task_manager.py:372-389` 的 `_set_web_image_task()` 每次状态更新都会调用该持久化函数。
- `main.py` 的视频轮询、队列等待、进度和终态路径多次调用 `_set_web_image_task`（例如 `2847-3007、3848-4223、5046-5250`）。

**当前行为与风险**：一次任务会产生大量完整 JSON 重写；实际写入次数、单次耗时和并发下锁等待尚未在生产数据中量化，不能直接断言瓶颈。

**优化方向**：先记录写入次数/字节数/耗时，评估节流、合并写入或 SQLite/WAL 事件存储；保留原子替换、重启后 queued/running 标记 expired、取消、配额释放和任务查询语义。

**验证**：新增状态更新密集、进程重启恢复、并发任务和磁盘写入失败测试；比较延迟、丢状态和文件损坏率。

### P0：画布会话整文件重写

**代码证据**

- `studio/studio.py:1387-1395` 创建两个独立 `StudioStore`，分别保存关系画布和创作画布 JSON。
- `studio/studio.py:1547-1557` 的 `_persist()` 排序全部会话并写完整 `studio_sessions.json`。
- 新增/删除/连接节点、槽位、保存和运行结果等路径均调用 `_persist()`（例如 `1709-1806、1953-2014、2070-2144`）。当前限制是 `MAX_SESSIONS=40`、`MAX_SLOTS=12`、`MAX_RESULTS_KEEP=24`、`CANVAS_MAX_NODES=128`（`24-28`）。

**当前行为与风险**：单个节点或槽位变化会重写该画布命名空间的全部会话；真实文件大小、频率和锁等待需观测。

**优化方向**：评估按会话/revision 写入或迁移 SQLite；兼容旧 JSON、两个命名空间、节点关系、结果保留上限及并发锁。

**验证**：旧 JSON 导入、交错更新两个画布、进程崩溃恢复、删除会话和上限裁剪测试；确认不会跨画布读取或丢失节点边。

### P0：缓存清理重复遍历整个目录

**代码证据**

- `generation/generation_store.py:1510-1522` 的 `_cache_stats()` 每次 `os.walk(generated_dir)` 统计总大小和文件数。
- `generation/generation_store.py:1546-1573` 构造清理计划时再次读取记录、画布引用并扫描候选文件。
- 清理入口在启动 `main.py:377-382`，以及生成请求/图片/视频完成路径 `main.py:1670-1672、1945-1953、3058-3062、5274-5304`。
- 记录收藏/置顶和两个画布会话的媒体保护分别在 `generation/generation_store.py:1524-1544`、`studio/studio.py:1590-1638`。

**当前行为与风险**：一次请求可能触发全目录统计和候选扫描；目录规模、清理耗时和并发重入次数尚未量化。

**优化方向**：评估维护文件计数/大小索引、增量 GC 或后台低优先级清理。必须保留记录引用、收藏、置顶、两个存活画布会话保护；删除画布会话后，下次清理仍应允许回收其路径。

**验证**：构造大缓存目录，覆盖请求前/生成后/启动清理、并发清理、保护路径和删除会话后的回收测试。

### P0：视频轮询首轮固定等待

**代码证据**：`generation/video.py:470-525` 的 `_poll_task()` 在第一次 GET 前先执行 `sleep(min(10, ...))`（`481-483`）。

**当前行为与风险**：即使上游已立即完成，也至少等待这一轮；首轮延迟需用 mock 或真实请求日志量化。

**优化方向**：提交后立即查询，后续使用固定或指数退避。保留 400/401/403/404/422 终止逻辑、全局超时和“不重复 POST”规则。

**验证**：mock 服务记录首个 GET 时间、请求次数、成功/失败/超时分支，并覆盖重试间隔。

### P1：视频下载整段读入内存

**代码证据**：`generation/video.py:388-458` 的 aiohttp 和 urllib 分支均调用 `response.read()`/`resp.read()` 返回完整 bytes；`628-632` 再一次性写入 mp4。

**优化方向**：下载至同目录临时文件后原子 `rename`，增加可配置大小上限和失败清理；保留 JSON/HTML 返回识别、aiohttp/urllib fallback、代理行为和 data URL 支持。不要假设上游一定提供 `Content-Length`。

**验证**：大响应、断流、JSON/HTML 错误、代理 fallback、磁盘不足和临时文件清理测试。

### P1：视频传输实现分散，保留旧 Agnes 路径

**代码证据**：通用下载在 `generation/video.py:388-458`；Agnes 创建/轮询在 `1066-1273`；`1276-1302` 的 `_poll_agnes_task()` 明确标注 deprecated。

**优化方向**：抽取统一 transport adapter，但保留协议差异；先统计各协议真实调用和错误，再决定 deprecated 入口的退场策略，不能仅因重复代码删除旧轮询。

**验证**：为每种 provider/代理/相对 URL 建立协议矩阵，确认回退顺序、错误分类和记录字段不变。

### P1：命令入口和任务生命周期集中在超大 `main.py`

**代码证据**：`main.py` 约 7147 行；视频命令包装器在 `3665-3694`，视频预设/配置/工具处理在 `6802-7099`，图片/视频/自拍/画布任务入口分散在多个区段。

**优化方向**：抽取统一 command specification、参数解析和任务启动层，先建立命令行为矩阵再做结构重构。

**不可改变契约**：命令别名、视频 `-c` 数量解析、队列/取消、额度、审核、provider 回退、记录来源和任务状态。

**验证**：逐命令比较解析结果、任务快照、记录来源和错误消息；完成后跑全量测试。

### P1：模型 target 每次配置读取都重新构造

**代码证据**：`core/models.py:570-635` 每次获取图片/审核/视频 target 都遍历 channel 并排序；`538-568` 的 `_bind_download_proxies()` 为 target 深拷贝并附加下载代理；配置应用 `features/config_manager.py:516-530` 会重建 semaphore。

**优化方向**：评估基于配置 revision 的 target cache。配置、代理、优先级、模型密钥和并发参数变化时必须失效，不能复用旧密钥或旧代理。

**验证**：配置更新、随机/固定模式、优先级过滤、下载代理变化和并发变更测试，并比较构造耗时。

### P1：多个小型 JSON 存储重复格式化写入

**代码证据**：`core/utils.py:46-62` 的 `save_json_file()` 使用 `indent=2`，临时文件后 `os.replace`。调用点包括 usage `main.py:405-407`、Web 任务 `tasks/task_manager.py:155-158`、画布 `studio/studio.py:1547-1557`、预设 `prompts/preset.py:178-192`、COS 池 `cos/cos_pool.py:86-87`、persona `features/persona.py:516`、渠道健康历史 `generation/generation_store.py:1858-1862`。

**优化方向**：评估统一异步/批量写入器，或按存储类型迁移 SQLite。保留原子替换、备份、迁移和敏感字段脱敏约束。

**验证**：逐存储类型覆盖并发写入、崩溃恢复、迁移回退和数据一致性；未测量前不承诺性能收益。

### P2：代理质量检查重复建 session 且 fallback 串行

**代码证据**：`core/proxy.py:199-238` 每次 `_request_via_proxy()` 新建 `aiohttp.ClientSession`；`241-302` 的出口 IP/地理探测最多依次尝试三个地址；`363` 起质量检查按目标逐个请求。

**优化方向**：先记录探测耗时和命中率，再评估复用 session、并行探测或成功短路；不得改变代理协议、认证和超时语义。

**验证**：代理类型、认证、失败地址、超时和资源释放测试。

### P2：Dashboard 单 HTML 与生成 fallback 双产物

**代码证据**：`pages/dashboard/index.html` 约 9381 行，内联 CSS/JS；`webui/index_fallback.py` 是由 `scripts/generate_dashboard_fallback.py` 生成的完整 HTML 字符串。

**优化方向**：低优先级评估按页面/模块拆分或构建 bundle。任何拆分都必须保持嵌入页、独立 Web、fallback 和缓存行为一致；fallback 只能由脚本生成，不能手工分叉。

**验证**：生成脚本与检查脚本、两种入口（嵌入/独立）和缓存命中回归。

## 3. 不列为待办的已完成能力

以下能力已在当前代码和测试中存在，后续优化不得重复实现或误报为缺陷：

- 视频默认并发为 3，并通过视频 semaphore/队列等待字段排队（`main.py:356-357、4092-4146`）。
- 视频命令支持 `-c` 批量数量，并保留任务级 requested/success 统计（现有视频命令测试覆盖）。
- 关系画布和创作画布均参与媒体缓存保护；删除画布会话后，保护路径会在下一次清理重新计算（`generation/generation_store.py:1524-1544`、`studio/studio.py:1590-1638`）。
- Dashboard fallback 由脚本生成并可校验，不能直接编辑生成文件。

## 4. 开发与发布要求

1. 每次只选择一个 backlog 项，先提交观测指标和失败用例，再改实现。
2. 不猜测上游返回格式；协议差异须用现有代码、抓包或 mock 证明。
3. 发布前在本地完成测试和静态检查，再推送 `origin/main`；服务器开发仓库 `/home/astrbot_plugin_selfie_image` 只做快进同步。
4. 服务器安装目录与开发仓库分开备份；不要删除运行数据、任务记录或媒体缓存。
5. 回滚只恢复代码提交，保留运行数据和缓存。

## 5. 固定验证命令

```bash
PYTHONPATH=.. pytest -q
python -m compileall -q core features generation tasks webui prompts studio cos main.py
python scripts/generate_dashboard_fallback.py
python scripts/check_dashboard_fallback.py
git diff --check
```

## 6. Hermes `/goal` 首轮方案

发送到服务器已启动的 Hermes screen 会话：

```text
/goal 读取 /home/astrbot_plugin_selfie_image 当前代码和新的 OPTIMIZATION_PLAN.md，只实现计划中有明确代码证据的一项优化。先为该项补充回归测试和可量化基线，再改实现；不要猜测上游协议，不删除兼容路径，不改变命令别名、-c 数量解析、队列、取消、额度、审核、回退和记录语义。完成后运行 PYTHONPATH=.. pytest -q、compileall、generate_dashboard_fallback.py 和 check_dashboard_fallback.py，并汇报改动、测试结果和剩余风险。首轮从 P0 的 Web 任务持久化、缓存清理、视频轮询三项中选择一项，避免同时大改。
```
