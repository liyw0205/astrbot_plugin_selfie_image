# Selfie 引入 infinite-canvas 特性 — 开发方案

> 参考仓库：https://github.com/basketikun/infinite-canvas（v0.15.1，浅克隆于 `/tmp/canvas_survey/infinite-canvas`）  
> 目标仓库：`astrbot_plugin_selfie_image`（当前约 1.3.17）  
> 日期：2026-08-09  
> 原则：**借鉴能力与交互，不整仓搬迁**；保留 Selfie 的 QQ 指令 / LLM 工具 / 人设合影 / 服务端渠道体系。

## 已拍板（2026-08-09）

1. Tab 名：**画布**
2. 默认 **合影模板**（形象 + 同框对象 + 可选场景）
3. 提示词库：**不要**同步外部 GitHub，**内置芯片**即可
4. Phase A：**先只图片**（视频后续）


---

## 1. 参考项目速览

### 1.1 产品定位

infinite-canvas 是**浏览器端图片创作工作台**，把「无限画布编排 + AI 生图/视频 + 参考图编辑 + 助手 + 提示词库 + 素材」放在同一 UI。

技术栈：`web/` 为 Vite + React 19 + Zustand + localForage + Ant Design；**AI 请求默认浏览器直连** OpenAI 兼容接口；媒体 Blob 本地 IndexedDB；可选本地 Canvas Agent / Codex 插件。

### 1.2 核心能力清单（值得借鉴）

| 能力块 | 要点 |
|--------|------|
| 无限画布 | 多项目、拖拽缩放、连线、小地图、撤销重做、导入导出 JSON |
| 节点 | 图片 / 文本 / 生成配置 / 视频（插件可扩 Markdown/SVG/…） |
| 图片工作流 | 上传、拖入、裁剪、多角度变换、失败重试、多图组节点 |
| AI 生成 | 文生图 `images/generations`、图生图 `images/edits`、视频任务轮询、自定义调用脚本 |
| 引用语法 | 提示词里 `@` 引用上游图/文/视频，输入框显示缩略图 chip |
| 生成配置节点 | 汇总上游输入、`inputOrder`、模型/比例/数量、批量生成 |
| 画布助手 | 选中节点 + 上游引用，对话/生图结果插回画布 |
| 提示词库 | 前端拉 GitHub 开源仓库，IndexedDB 缓存，搜索标签 |
| 素材库 | 本地「我的素材」，与画布节点解耦存储 |
| 插件系统 | 远程 URL 装节点插件 + TS SDK（Selfie 短期可不做） |
| Agent | 本机 MCP 操作画布（Selfie 短期可不做） |

### 1.3 数据结构（精简）

```
CanvasProject { id, title, nodes[], connections[], chatSessions[], viewport, backgroundMode }
CanvasNodeData { id, type: image|text|config|video, title, position, width, height, metadata }
CanvasConnection { id, fromNodeId, toNodeId }
metadata: prompt, status, model, size, count, images[], primaryImageId, inputOrder, storageKey, ...
```

媒体：JSON 只存 `storageKey` + 展示 URL；Blob 进 `image_files` / `media_files`；删除走引用计数清理。

### 1.4 与 Selfie 的本质差异

| 维度 | infinite-canvas | Selfie |
|------|-----------------|--------|
| 形态 | 独立 SPA 工作台 | AstrBot 插件 + QQ/LLM + Flask Dashboard |
| AI 调用 | 浏览器直连 Key | 服务端渠道（多 key、fallback、审核、配额） |
| 状态 | 浏览器 localForage | 服务端 `plugin_data` + 生成记录 |
| 主场景 | 连续视觉迭代 | 对话触发生图 / 自拍 / 合影 / 晒腿 |
| UI | React 重前端 | 单页 `INDEX_HTML`（KISS） |

**结论：禁止整仓嵌入。** 应拆「可复用交互模式」接到 Selfie 现有生图管线，而不是 fork 无限画布。

---

## 2. 目标与非目标

### 2.1 目标

1. 在 Selfie **管理端**提供「视觉迭代工作区」：多参考图编排 → 生成 → 结果回插 → 再编辑。  
2. 复用 Selfie 已有：`image_channels` / `video_channels` / `generate_image_with_fallback` / 记录 / 形象参考。  
3. 与 QQ 指令解耦：画布是 **Web 能力**；指令仍走现有 persona 流程。  
4. 分阶段交付，每阶段可独立上线。

### 2.2 非目标（明确不做或远期）

- 不做完整 React 无限画布 SPA 替换 Dashboard。  
- 不做 Codex / Claude Agent MCP。  
- 不做节点插件市场 / TS SDK。  
- 不做浏览器直连用户 Key 绕过服务端（安全与配额会穿）。  
- 不把 QQ 群聊变成画布实时协同。

---

## 3. 可移植特性分级

### P0 — 高价值、与 Selfie 契合（建议做）

| # | 特性 | 映射到 Selfie | 价值 |
|---|------|---------------|------|
| P0-1 | **多参考图工作台** | 新 Dashboard Tab「画布/编排」：多图槽位 + 主图 + 顺序 | 合影/换装/COS 参考管理弱 |
| P0-2 | **生成配置面板** | 模型（走现有渠道优先级/随机）、比例、数量、模式 t2i/i2i | 试画页升级 |
| P0-3 | **结果回插与分支** | 生成图进入槽位，可选「以此为参考继续」 | 连续迭代 |
| P0-4 | **失败重试** | 单槽位 retry，复用 attempts 信息 | 已有 partial，UI 强化 |
| P0-5 | **本地素材/形象库入口** | 形象参考 + 记录图一键加入工作区 | 减少重复上传 |

### P1 — 中价值（第二阶段）

| # | 特性 | 说明 |
|---|------|------|
| P1-1 | 轻量「节点连线」语义 | 不必真无限画布：用「输入卡 → 输出卡」列表 + 顺序拖拽即可 |
| P1-2 | 提示词库 | 内置/导入 JSON；可选同步 1～2 个开源 prompt 仓（注意合规） |
| P1-3 | 图片裁剪 / 简单变换 | 浏览器 canvas 裁剪后作为新参考图上传 |
| P1-4 | 多图结果组 | 一次 N 张折叠预览、选主图 |
| P1-5 | 项目导入导出 | 工作区 JSON + 图 zip（服务端或浏览器） |

### P2 — 低优先级 / 重成本

| # | 特性 | 原因 |
|---|------|------|
| P2-1 | 真·无限画布（pan/zoom/minimap） | 前端体量大，与现 INDEX_HTML 架构冲突 |
| P2-2 | 画布右侧 Agent 对话 | 与 AstrBot 主对话重复；可远期「工作区备注」 |
| P2-3 | 节点插件系统 | 维护成本高 |
| P2-4 | 视频节点全流程 | Selfie 已有 `/视频`；画布侧可只做入口链到现有 video |

---

## 4. 推荐架构（Selfie 化）

### 4.1 总体

```
QQ/LLM 指令 ──► 现有 main/persona/generator（不变）
                      │
Web Dashboard ──► [新] Studio 工作区 Tab
                      │
                      ▼
              StudioSession (服务端 JSON)
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
    slots/refs    run_generate   assets bridge
         │            │            │
         └────► generate_image_with_fallback / video
                      │
                      ▼
              generation_records + image_cache
```

### 4.2 数据模型（建议新增）

文件：`plugin_data/.../studio_sessions.json` 或按 session 分文件。

```json
{
  "id": "studio-...",
  "title": "合影迭代 08-09",
  "updated_at": "...",
  "slots": [
    {
      "id": "s1",
      "role": "identity|subject|style|result|extra",
      "label": "形象",
      "image_path": "image_cache/...",
      "source": "persona|upload|record|generated"
    }
  ],
  "graph": {
    "prompt": "...",
    "mode": "i2i|t2i|group|selfie",
    "aspect_ratio": "自动",
    "resolution": "1K",
    "count": 1,
    "input_order": ["s1", "s2"],
    "channel_policy": "priority|random|fixed",
    "fixed_target": ""
  },
  "runs": [
    {
      "id": "run-...",
      "status": "running|ok|fail",
      "task_id": "cmd-...",
      "result_paths": [],
      "error": "",
      "attempts": []
    }
  ]
}
```

对应 infinite-canvas 概念：

| canvas | Selfie Studio |
|--------|---------------|
| image node | slot |
| text/prompt | graph.prompt |
| config node | graph + 生成按钮 |
| connection / inputOrder | input_order |
| project | studio session |
| blob storageKey | image_cache 相对路径 |

### 4.3 API（Flask 扩展，均需 access）

| Method | Path | 作用 |
|--------|------|------|
| GET/POST | `/api/studio/sessions` | 列表 / 新建 |
| GET/PATCH/DELETE | `/api/studio/sessions/:id` | 读写删 |
| POST | `/api/studio/sessions/:id/slots` | 上传或从记录/形象导入 |
| POST | `/api/studio/sessions/:id/run` | 触发生成（复用后台 task） |
| GET | `/api/studio/sessions/:id/runs/:run_id` | 轮询 |
| POST | `/api/studio/sessions/:id/promote` | 结果写入形象或素材 |

**禁止**前端直连渠道 Key；一律服务端 adapter。

### 4.4 UI 落点

在现有 Dashboard tabs 增加 **「编排」**（或「画布」）：

1. 左：槽位条（可拖排序 = input_order）  
2. 中：主预览 + 结果历史条  
3. 右/下：提示词、模式、比例、数量、渠道策略、生成  
4. 不引入完整 pan/zoom 画布；用**卡片流**模拟节点图（P0）  
5. P1 再考虑简易 2D 摆放（CSS transform），仍非 React Flow 级

风格延续现有 `INDEX_HTML` 变量与 autosave 纪律（弹窗不乱 toast；保存明确）。

---

## 5. 分阶段实施

### Phase A — Studio MVP（约 1～2 周有效工时）

**交付**

- Tab「编排」  
- 多图槽位（上传 / 从形象 / 从最近记录）  
- 提示词 + 比例 + 数量 + 生成  
- 走现有 `_run_image_generation` / command task  
- 结果进槽位与 `generation_records`（source=`studio-run`）  
- 失败展示单因 + 重试按钮  

**验收**

- 3 张参考图 i2i 成功  
- 无 Key 泄露到前端  
- 记录可在「记录」Tab 筛到  
- 单测：session CRUD、slot order、run 状态机  

### Phase B — 迭代工作流（约 1 周）

- 「以此为参考继续」一键把 result → subject/identity  
- 多结果组 + 选主图  
- 简单裁剪（浏览器）  
- 导出 session JSON（路径列表，不含大图 base64）  

### Phase C — 提示词与视频入口（约 1 周）

- 本地提示词库（JSON 文件 + 搜索）  
- 可选：只读同步 1 个开源 prompt 仓（缓存服务端，不浏览器直拉以免 CORS/配额）  
- 编排页「生成视频」调用现有 video 管线  

### Phase D — 可选增强（评估后再做）

- 轻量 2D 自由摆放  
- 合影专用槽位模板（identity + peers[] + scene）  
- WebDAV/备份（一般不需要，已有 plugin_data 备份习惯）  

---

## 6. 与现有 Selfie 能力对齐

| 现有 | Studio 用法 |
|------|-------------|
| `image_channels` / 优先级 / `random_image_model` | graph.channel_policy |
| `persona` 形象图 | 默认填 identity 槽 |
| 合影/换装提示词 | 模式预设：group / clothes / bare（调用现有 `_build_*_action` 或简化版） |
| `generation_records` | source=`studio-*` |
| `video_channels` | Phase C |
| 审核 / 日限 / 信号量 | run 路径必须走同一 `_run_image_generation` |
| 自然 ack 文案 | Web 用状态条，不套 QQ「已接单」 |

---

## 7. 风险与约束

1. **前端体量**：INDEX_HTML 已大；Studio 建议独立 `pages/studio/` 静态资源或分块脚本，避免再塞巨型内联。  
2. **协议版权**：借鉴交互与数据思路即可；**不要**复制 infinite-canvas 大段源码；UI 自研。保留其 MIT 声明若有直接引用。  
3. **存储膨胀**：槽位图走 image_cache + 引用清理（已有 cache limit）。  
4. **并发**：run 必须进现有 semaphore / global_timeout。  
5. **安全**：Web API 继续 access token；不把 api_key 下发浏览器。  
6. **兼容**：Studio 失败不影响 QQ 指令主路径。  

---

## 8. 建议优先级（一句话路线）

```
A 多参考编排+生成回插
 → B 结果再编辑/裁剪/主图
 → C 提示词库+视频入口
 → D 真画布化（仅当 A–C 证明刚需）
```

**首期不要做**无限画布引擎；用「槽位 + 顺序 + 生成配置」吃掉 80% 参考项目对 Selfie 有用的创作闭环。

---

## 9. 参考文件（调研用）

| 路径 | 用途 |
|------|------|
| `infinite-canvas/README.md` | 产品总览 |
| `docs/.../features.zh-CN.mdx` | 功能清单 |
| `docs/.../canvas-data-structure.zh-CN.mdx` | 存储模型 |
| `docs/.../canvas-node-manual.zh-CN.mdx` | 节点操作流 |
| `plugins/canvas/README.md` | 插件扩展（P2） |
| Selfie `web.py` / `main.py` / `generator.py` / `providers.py` | 接入点 |

---

## 10. 待你拍板

1. 首期 Tab 名称：**编排** / **画布** / **工作室**？  
2. 是否要「合影模板」作为默认 session 布局？  
3. 提示词库要不要同步外部 GitHub（有合规与维护成本）？  
4. Phase A 是否必须支持视频，还是纯图片？  

确认后可按 Phase A 直接开干（API + Tab + 单测 + 同步运行副本）。
