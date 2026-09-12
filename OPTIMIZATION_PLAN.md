# Selfie Image 关系网重构计划

> 本文是当前仓库 `7d39cac` 的实施计划，不把旧实验代码或愿望清单当成现状。
> 关系网前端只参考 [infinite-canvas](https://github.com/basketikun/infinite-canvas) 的无限画布交互和视觉结构；不复制其后端、数据模型或业务逻辑。后端必须继续复用本插件已有的会话、模板、任务、记录、缓存和权限能力。

## 1. 基线与边界

### 1.1 代码基线

- 本地和远程代码基线：`7d39cac fix selfie daily context injection`。
- 远程插件目录：`/root/AstrBot/data/plugins/astrbot_plugin_selfie_image`。
- 运行数据目录：`/root/AstrBot/data/plugin_data/astrbot_plugin_selfie_image`，代码回退不得删除或重置该目录。
- 前端源文件：`pages/dashboard/index.html`；`webui/index_fallback.py` 只能由脚本生成，不能直接手改。
- 服务层入口：`webui/services.py`、`webui/dashboard_api.py`、`studio/studio.py`、`studio/studio_adapter.py`。

### 1.2 产品边界

关系网页面只负责“看清关系、选择节点、对节点执行动作”：

- 进入页面先选择一个已有会话；选择后自动读取该会话的节点和边。
- 画布是横向无限画布，节点以媒体缩略图为主，边表示父子生成关系；不再用文字列表替代关系网。
- 空白区域拖拽平移，滚轮以鼠标位置为中心缩放，双击节点聚焦；画布可直接操作，不依赖缩放按钮。
- 单击节点打开节点窗口/侧栏，显示模板参数、生成状态、图片结果和父节点继承来源。
- 节点窗口支持选择模板、编辑参数、生图、重画、删除；默认模板参数继承上一节点，可逐项覆盖。
- 新建节点是画布内唯一显式创建入口，默认从当前选中节点创建；首次会话自动设置起始节点。
- 删除节点默认只删除节点及其关联边，删除前必须确认；重画生成新任务并保留原节点结果，避免历史记录断链。
- 关系网不在主视图堆放高级 Canvas API、批量工具、任务诊断或配置表单；这些能力保留在现有页面和 API。

不在本轮做：复制 infinite-canvas 的后端、替换现有生成器/provider、迁移历史记录数据库、把普通资产库改成关系网、删除 Flask 兼容入口。

## 2. 参考实现提炼

只吸收以下前端原则：

| 参考原则 | 本插件落地方式 |
| --- | --- |
| 单一 viewport (`translate + scale`) | 关系网容器维护 `panX/panY/zoom`，节点坐标在世界坐标系保存 |
| 空白拖拽平移 | `pointerdown` 命中空白后进入 pan，移动端使用触摸拖拽 |
| 鼠标中心缩放 | 根据指针在画布中的世界坐标修正平移，限制合理最小/最大缩放 |
| 节点与连接线分层渲染 | 连接线层使用 SVG，节点层使用 HTML；选中节点和相邻边统一高亮 |
| 媒体优先节点 | 缩略图、状态、短标题和生成时间为主信息，完整参数进入节点侧栏 |
| 侧边面板 | 节点列表、搜索和详情在侧栏承载，避免遮挡画布 |
| hover/selected 工具条 | 只显示删除、重画、新建等当前节点动作；不显示大量全局按钮 |
| 网格跟随 viewport | 背景网格使用 viewport 变换，平移缩放时保持空间感 |

不直接复制其实现细节、依赖、后端 API、项目/账户模型或视觉资源。

## 3. 现有后端能力复用

在新增关系网 API 前先确认并复用这些事实来源：

| 能力 | 现有事实来源 | 关系网用途 |
| --- | --- | --- |
| 会话/槽位/模板 | `studio/studio.py`、`studio/studio_adapter.py` | 会话选择、起始节点、模板选择和参数默认值 |
| 生图任务 | `tasks/task_manager.py`、`main.py` | 创建、轮询、取消、重试和终态处理 |
| 生成记录 | `generation/record_database.py`、`generation/generation_store.py` | 图片结果、参数快照、来源记录和权限校验 |
| 媒体 URL | `webui/services.py` 及现有媒体路由 | 节点缩略图和结果预览，必须继续鉴权 |
| 画布持久化 | 当前 Studio 数据目录和 adapter | 节点/边/viewport 的保存与恢复 |
| 模板与提示词 | `prompts/`、`features/creative_features.py` | 节点窗口的模板参数和上一节点继承 |

任何新增字段都必须有默认值，旧会话缺字段时仍可打开；任何生成动作必须走现有额度、审核、provider 回退、记录和缓存链路。

## 4. 数据契约

### 4.1 关系网文档

```json
{
  "session_id": "string",
  "version": 1,
  "root_node_id": "string|null",
  "viewport": {"x": 0, "y": 0, "zoom": 1},
  "nodes": [
    {
      "id": "string",
      "parent_id": "string|null",
      "record_id": "string|null",
      "title": "string",
      "template_id": "string|null",
      "params": {},
      "position": {"x": 0, "y": 0},
      "status": "draft|queued|running|succeeded|failed|deleted",
      "result": {"media_url": "string|null", "thumbnail_url": "string|null"},
      "created_at": "ISO-8601"
    }
  ],
  "edges": [{"id": "string", "source": "string", "target": "string"}]
}
```

- `parent_id` 是业务关系，`edges` 是渲染快照；服务端保存时必须校验节点存在、边无自环、父子关系一致。
- 节点位置是用户可调整的显示状态，不影响生图参数。
- `record_id` 缺失或媒体失效时仍渲染节点，并显示可重画/删除状态。
- 服务端生成 `media_url`，前端不拼接文件路径。

### 4.2 API 方向

保持现有认证和错误 envelope，具体路由以服务层现有命名为准：

- `GET canvas/sessions`：返回可选会话摘要，不把所有节点塞进列表。
- `GET canvas/sessions/{id}/graph`：返回关系网文档；不存在时返回空起始状态。
- `PATCH canvas/sessions/{id}/graph`：只更新节点位置、删除结果确认后的结构和 viewport；服务端做版本/字段校验。
- `POST canvas/sessions/{id}/nodes`：从父节点和模板参数创建节点并提交现有生图任务。
- `POST canvas/nodes/{id}/regenerate`：用覆盖后的模板参数提交新任务，保留旧记录链路。
- `DELETE canvas/nodes/{id}`：确认后删除节点及关联边，不删除仍被其他记录引用的媒体。
- `GET canvas/nodes/{id}`：返回模板参数、继承来源、任务状态和结果详情。

重复提交必须使用现有任务去重或幂等字段；权限、额度、审核、缓存和错误处理不在前端重写。

## 5. 实施阶段

### Phase 0：基线与观测（P0，BE/FE）

- 同步仓库基线到远程并重启服务，确认旧会话数据可读。
- 记录现有 Studio API、页面入口和媒体鉴权行为。
- 为关系网 API 增加请求日志摘要（不记录提示词、Key、Cookie 或原图）。

验收：远程代码关键文件与本地一致；运行数据目录未被覆盖；旧页面和旧命令可用。

### Phase 1：后端关系网契约（P1，BE/DATA）

- 从现有 Studio 会话/槽位/记录构建节点和边的只读投影。
- 增加版本化 graph 文档、位置和 viewport 持久化；兼容空 graph 和旧字段。
- 实现会话列表、graph 查询、节点详情及结构更新；生成动作先接入现有 task runner。

验收：同一会话刷新、重启后节点/边/位置一致；缺失媒体不导致整个 graph 失败；非法边、未知节点和越权请求返回明确 `4xx`。

### Phase 2：无限画布前端（P1，FE）

- 在 `pages/dashboard/index.html` 的关系网页面实现 viewport、网格、SVG 连线和媒体节点。
- 使用 pointer events 支持空白拖拽、节点选择、节点拖动、鼠标中心缩放和触摸操作。
- 会话选择置于轻量顶部栏；节点列表/搜索/详情放在可收起侧栏。
- 移除旧的文字关系网、固定缩放依赖和主视图高级按钮；保留无 JS/fallback 可访问性。

验收：桌面和 390x844 移动视口可操作；缩放和平移不改变节点业务数据；节点和相邻边选中状态清楚；无内容重叠和横向溢出。

### Phase 3：节点窗口与生成闭环（P1，FE/BE/E2E）

- 节点窗口展示模板参数、继承来源、图片结果、任务进度和错误。
- 新建/重画默认继承上一节点参数；模板切换后只覆盖模板定义字段。
- 删除、重画、关闭窗口、任务轮询和刷新恢复均使用明确的 pending/success/error 状态。

验收：节点生图完成后自动更新缩略图和边；重复点击只创建一个任务；重画保留旧记录；失败可重试；删除不会误删共享媒体。

### Phase 4：真实浏览器验收与发布（P0，E2E）

- 使用 Termux Chromium 真实打开远程页面，不使用 API 假数据替代页面操作。
- 覆盖登录、首次提示关闭、进入关系网、选择会话、自动连线、拖拽、缩放、选择节点、详情、模板参数、生成、重画、删除和刷新。
- 记录桌面/移动截图、浏览器 console 错误、关键请求状态和任务 ID；必要时修复后重复测试。
- 同步最终代码并重启 `/root/astr.sh restart`，检查 HTTP 200 和插件加载日志。

## 6. 验收矩阵

| 场景 | 期望结果 |
| --- | --- |
| 新会话 | 自动生成一个起始节点，画布横向居中，不需要手动连线 |
| 多分支 | 子节点可在父节点上方或下方，连接线不遮挡节点内容 |
| 空白拖拽 | 只改变 viewport，不触发节点选择或创建 |
| 鼠标滚轮/触摸缩放 | 以指针/双指中心缩放，节点不会跳到画布外 |
| 节点点击 | 只打开该节点窗口并高亮相邻边，其他节点仍可见 |
| 模板继承 | 默认显示上一节点参数来源，覆盖项清晰可见 |
| 生图/重画 | 使用现有任务、额度、审核、记录和缓存链路，状态可恢复 |
| 删除 | 需确认，边同步消失，共享媒体和其他记录不受影响 |
| 刷新/重启 | 会话、节点、边、位置和任务状态可恢复，旧字段可读 |
| 错误/空状态 | 显示可操作错误或空状态，不出现无响应按钮 |

## 7. 验证命令与证据

```bash
PYTHONPATH=.. pytest -q
python -m compileall -q core features generation tasks webui prompts studio cos main.py
python scripts/generate_dashboard_fallback.py
python scripts/check_dashboard_fallback.py
git diff --check
```

关系网改动额外必须提供：

- API 契约测试：空会话、旧 graph、非法节点/边、越权、重复提交和任务失败。
- 浏览器实测记录：URL、设备视口、操作步骤、预期/实际、截图路径和 console 错误。
- 发布前远程备份；回滚只替换代码目录，不删除 `plugin_data`。

## 8. 当前看板

| 项目 | 范围 | 状态 |
| --- | --- | --- |
| 远程代码回退到 `7d39cac` | 发布 | done |
| 本计划重写并明确前后端边界 | 文档 | done |
| 关系网 graph 读写契约 | BE/DATA | ready |
| 无限画布与横向节点布局 | FE | ready |
| 节点窗口与生成闭环 | FE/BE/E2E | pending |
| Termux Chromium 桌面/移动实测 | E2E | pending |

完成标准不是“文件已修改”，而是关系网从会话读取到生成结果回写的完整链路在真实浏览器中可重复操作，并保留现有插件后端能力和可回滚数据。
