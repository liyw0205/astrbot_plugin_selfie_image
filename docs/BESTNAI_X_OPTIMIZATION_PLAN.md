# 参考 bestnai_x（NAI + 无限画布）— Selfie 优化方案

> 参考仓库：https://github.com/Menkelo/astrbot_plugin_bestnai_x（v3.3.6，浅克隆 `/tmp/nai_canvas_survey/astrbot_plugin_bestnai_x`）  
> 目标：`astrbot_plugin_selfie_image`（当前约 1.3.24）  
> 日期：2026-08-09  
> 原则：**借鉴能力与产品细节，不整仓搬迁**；保留 Selfie 多渠道、人设合影、LLM 工具、服务端鉴权与审核。

---

## 1. bestnai_x 是什么

| 项 | 内容 |
|----|------|
| 定位 | AstrBot 上的 **NovelAI 风格生图** 插件（固定 `nai-diffusion-4-5-full`）+ **Infinite Canvas 工作台** |
| 指令 | `/nai`、`/nai0`、画师画廊/预设 |
| 画布 | 独立 Page：`pages/canvas/`（`canvas.js` ~5k 行 + `manager.js` + CSS），服务端 `services/canvas.py` ~1.5k 行 |
| 依赖 | AstrBot **≥4.26.0**（`astrbot.api.web` 注册 Canvas Page） |
| 画布来源 | 适配 [hero8152/Infinite-Canvas](https://github.com/hero8152/Infinite-Canvas)（见其 THIRD_PARTY_NOTICES） |
| 生图 | OpenAI 兼容中转；`/images/generations` 失败回退 `/chat/completions`；返回 seed |
| 强项 | 反推 tags、标签图层冲突合并、NAI PNG 元数据复用、画师串、中译 Danbooru、画布多工作区持久化 |

**不是**完整官方 NovelAI vibe/inpaint 全家桶，而是「NAI 中转 + tag 工作流 + 真无限画布」。

---

## 2. 与 Selfie 对照

| 维度 | bestnai_x | Selfie（现状） |
|------|-----------|----------------|
| 产品 | 二次元 NAI tag 工作流 | 多模态写实/自拍/合影/换装 + 多厂商 |
| 渠道 | 基本单协议 NAI 中转 | OpenAI/Gemini/Grok/Agnes/**NovelAI**/… 多渠道 fallback |
| 画布 UI | 真无限画布（节点+连线+缩放平移+撤销） | Dashboard **「画布」Tab**：模板槽位编排（Phase A） |
| 画布存储 | 多工作区、节点/连线上限、素材 GC | `studio` 会话 JSON + 槽位图路径 |
| 参考图 | 图→反推 tags→图层 merge | 原图 bytes 进 edits / 角色 ref，**不做 tag 反推** |
| 身份 | 角色 tag / 画师串 | **形象参考图** + persona 文案锁脸 |
| 预设 | 画师预设 + 画廊 | 全局风格预设（捧脸/变真人…）+ 模板芯片 |
| 元数据 | 读 NAI PNG seed/prompt | 基本不读；生成记录有 prompt/model |
| 调试 | 画布顶栏 debug 流水 | 渠道监控记录为主 |
| 安全 | QQ 提示词过滤 + 出图视觉审核 | 屏蔽词/审核渠道（可配） |
| 宿主 | 强依赖 4.26 Page | Flask Web + 内嵌 dashboard（更宽版本兼容） |

结论：

- **不要**把 bestnai 的整套 Canvas SPA 塞进 Selfie（体积大、协议绑定 NAI tag、AstrBot 版本门槛高）。
- **要**借它的：**迭代闭环、参数可复现、图层式改图语义、素材库、调试可见性、NAI 渠道参数深度**。
- Selfie 画布继续走「服务端会话 + 现有 generate 管线」，UI 可从槽位逐步增强，真无限画布列为可选远期。

---

## 3. 值得借鉴的能力（按价值）

### P0 — 对 Selfie 立刻有用

1. **结果回插 + 再编辑闭环**（bestnai：生成图变新 image 节点再连 prompt）  
   Selfie 已有「作同框/作形象」；应补齐：**结果一键变「底图/服装参考」**、**以上一张继续**固定链路。

2. **种子 / 请求参数回显**（bestnai：卡片显示 seed）  
   记录与画布结果卡展示：model、渠道、比例、耗时；NAI 渠道额外 **seed / steps / scale**（能拿到就存）。

3. **服装跟进语义的「图层」思想**（bestnai：clothing/pose 替换、identity 保留）  
   Selfie 不走 tag，但 prompt 侧已在做「锁脸迁衣」；可产品化成画布槽位角色：`identity` / `outfit` / `pose` / `scene`（已有雏形）+ **明确优先级文案**。

4. **素材库与会话解耦**（bestnai：素材库 → 放入画布）  
   Selfie：从「记录 / 形象 / 缓存」挑选进槽，避免只靠当场上传。

5. **画布任务不打断下载/预览**（bestnai：生成中锁下载按钮）  
   Selfie：生成中禁用危险操作，状态栏单行进度，避免重复点「开始生成」。

### P1 — NAI 与专业工作流

6. **NAI 渠道参数面板**  
   bestnai：sampler、steps、scale、uc、默认比例表（64 对齐）。  
   Selfie `provider_type=novelai`：Web 渠道高级项暴露 seed/steps/scale/negative；官方 zip 与网关 GET 已有，补齐**可配置与回传**。

7. **PNG 元数据读取（仅 NAI 产物）**  
   借鉴 `services/nai_metadata.py`：上传图若含可信 NAI 参数，画布/图生图可预填 prompt/seed（**不**把普通 Description 当 tag）。

8. **负面提示词 / 质量词分轨**  
   NAI/二次元渠道：全局 negative + 可选 quality；写实渠道保持现有自然语言 prompt，避免硬塞 booru 词。

9. **调试条（可选开关）**  
   画布/试画底部：本轮 refs 角色、最终 prompt 摘要、attempts、错误原文（脱敏）。对齐 bestnai debug，但默认关。

### P2 — 真无限画布 / 反推（慎重）

10. **真无限画布 Page**  
    条件：AstrBot 版本足够、愿意维护大前端。可 **独立子页**「高级画布」，与现 Tab 并存；协议仍走 Selfie 服务端 API，不浏览器直连 Key。

11. **视觉反推 tags**  
    对 NAI/二次元有用；对写实自拍收益低且费视觉模型。建议：**仅当渠道族=NAI 或用户显式「反推」** 时启用，不作为默认合影路径。

12. **画师预设画廊**  
    与 Selfie「风格预设」可合并概念：二次元用 artist 串，写实用自然语言预设；UI 统一「预设」，存储分 `style=nai_artist|natural`。

13. **中译 Danbooru + 检索**  
    仅 NAI 路径；Selfie 主路径保持中文自然语言 + 人设模板。

---

## 4. 明确不照搬

| bestnai 做法 | 原因 |
|--------------|------|
| 固定单模型 `nai-diffusion-4-5-full` | Selfie 是多模型市场插件 |
| 浏览器/画布侧弱化 QQ 安全 | Selfie QQ 指令与 LLM 必须保留审核 |
| 整份 Infinite-Canvas 前端（数千行 JS） | 维护成本高；Phase A 槽位已覆盖主需求 |
| 强制 tag 化所有中文 | 破坏自拍/合影写实语法 |
| 反推默认覆盖 identity | 与 Selfie「形象锁脸」冲突，只能按槽位角色选择性反推 |

---

## 5. 推荐落地路线（Selfie）

### Phase B1 — 画布工作流补强（不引入真无限画布）

- 结果卡动作：**用作底图 / 用作服装 / 用作形象 / 再生成**
- 槽位拖拽排序（已有 reorder API 可接 UI）
- 生成中状态机：防连点、任务进度与记录一致
- 从「记录」挑选图进当前会话槽位
- 会话列表：模板徽章、最近结果缩略图

### Phase B2 — 可复现与 NAI 深度

- `generation_records` / studio result 写入 `seed`（若上游返回）、`steps`、`provider_request` 摘要
- NovelAI 渠道表单：seed、steps、scale、negative、sampler（网关能力范围内）
- 可选：读取 NAI PNG 元数据预填

### Phase B3 — 素材库

- `media_library`：用户收藏图（来自记录/上传），按标签；画布「从素材放入」
- 与形象参考分离（形象=身份锚点；素材=可复用零件）

### Phase B4 — 可选「高级画布」

- 评估 AstrBot Page 能力与版本；若做：
  - **节点类型**：图 / 文 / 备注（先不做视频）
  - **连线语义**：图→文 = 参考；文→生成 = prompt
  - 存储仍服务端；生成仍 `generate_image_with_fallback`
  - 可参考 bestnai 的 workspace 上限、上传限制、GC 策略
- 许可：若复用 Infinite-Canvas / bestnai 前端片段，保留 NOTICE

### Phase N — NAI 专项（可与 B2 并行）

- 官方 API 参数对齐（sm、uc_preset、character prompts 若需要）
- 与 vibe 插件差异说明写进用户文档：Selfie=多渠道自拍中枢，不是 NAI 全家桶替代

---

## 6. 架构建议（保持 KISS）

```
QQ / LLM / Web试画 / 画布Tab
        │
        ▼
  ReferenceCollector + Persona（身份/换装/合影）
        │
        ▼
  generator + 多渠道 providers（含 novelai）
        │
        ▼
  records + image_cache + studio sessions
```

bestnai 的 retag/merge **不要**默认插入主链；可做可选预处理：

```
[可选] NAI 图元数据 / 反推 tags ──► 仅写入 prompt 草稿
[默认] 原图像素 ref + 自然语言 action
```

---

## 7. 与已有 infinite-canvas 方案的关系

| 来源 | 贡献 |
|------|------|
| basketikun/infinite-canvas | 通用节点、@ 引用、配置节点、浏览器工作台概念 → 已吸收为 Phase A 槽位画布 |
| Menkelo/bestnai_x | **AstrBot 内嵌真画布落地样板**、tag 图层改图、seed 复现、素材库、NAI 元数据、调试条 |

Selfie 路线：**先 B1–B3 把槽位画布用到「可连续创作」**；真无限画布仅当用户强需求且版本允许再 B4。

---

## 8. 成功标准（可验收）

1. 用户发服装图 → 画布/对话换装 → 再改脸/姿势时 **服装参考不丢、不误用 bot 自拍当服装**（上下文逻辑已修，画布需同等）
2. 任意一次画布生成，结果可 **一键再生成/改槽** 且参数可从记录读回
3. NAI 渠道用户能设 seed 并在记录中看到（上游支持时）
4. 不引入「必须 tag 才能出图」；写实自拍路径零 tag 依赖
5. README 仍保持用户向，不把本方案开发细节写进主 README（本文件放 `docs/`）

---

## 9. 建议实施顺序（若开工）

1. B1 结果回插动作 + 记录选图入槽  
2. B2 NAI 参数与 seed 回传  
3. B3 轻量素材库  
4. 评估 B4；同时按需加深 novelai provider  

---

## 10. 参考路径（本机浅克隆）

- `/tmp/nai_canvas_survey/astrbot_plugin_bestnai_x/README.md`
- `.../services/canvas.py`、`pages/canvas/canvas.js`
- `.../core/generator.py`、`services/nai_metadata.py`、`services/prompt_merge.py`
- Selfie 现状：`studio.py`、`web.py` 画布 Tab、`providers.py` novelai、`docs/INFINITE_CANVAS_FEATURE_PLAN.md`
