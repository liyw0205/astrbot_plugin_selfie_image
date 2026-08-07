# Selfie Image 视频能力方案（文生视频 / 图生视频）

> 日期：2026-08-07  
> 基线：`astrbot_plugin_selfie_image` **1.2.2**（当前无视频）  
> 市场：`cloud.astrbot.app` / 本地缓存 `plugins.json` ≈ **1531** 条  
> 原则：视频能力**集成进生图插件**是市场主流，不是另起独立「视频 Bot」；Selfie 可做**可选视频链路**，不牺牲自拍主线。

---

## 1. 市场结论（先分清「生成」和「解析」）

全库扫 `description/tags/name` 含 video/视频/t2v/i2v/sora 等约 **60+** 命中，但大半是：

| 类型 | 例子 | 与 Selfie 关系 |
|------|------|----------------|
| **B 站/抖音/链接解析、总结、下载** | bilibili、yt-dlp、parser、link_resolver、videos_analysis | **不借**（业务无关） |
| **理解/抽帧/监控** | Qwen-VL 视频理解、ipcam、video_reference_vision | **不借** |
| **生成：文生视频 / 图生视频** | 见下表 | **借协议与任务模型** |

### 1.1 真正「生视频」且常与生图一体的插件

| 插件 | 形态 | 指令/能力摘要 | 协议线索 |
|------|------|---------------|----------|
| **OmniDraw 万象画卷** | **图+自拍+视频一体**（最接近 Selfie 产品形态） | `/视频 [提示词]`；可多图参考；后台慢任务；独立 **video 链路/Provider** | OpenAI 风格 `…/videos/generations`；`VideoManager` 提交后轮询；参考图转 data URL；超时建议 ≥300s |
| **big_banana 大香蕉** | **图+视频一体** | `bnv` 文生/图生视频；LLM `video_generation` 工具；CogVideoX / Agnes / Gemini 等 | 智谱 `…/videos/generations` + 任务轮询；`duration/fps/size`；图生视频最多 1 张首帧 |
| **grok_suite** | 文/图/**图生视频** 全能 | suite 一体，非纯视频插件 | Grok/中转生态 |
| **agnes_image** | 图+视频 | 官方 Agnes 文档向；文生图/图生图/视频；LLM 工具 | 厂商原生 + 大图策略 |
| **jimengapi 即梦** | 生图+图生图+**文生视频** | 即梦 2 API | 厂商 API（非纯 OpenAI 视频） |
| **gitee_aiimg** | 生图为主，描述含视频相关体验 | 后台不阻塞对话等 | 偏生图任务机，视频非主卖点 |

### 1.2 独立/专用视频生成插件（可借协议，不必做成 Selfie 外壳）

| 插件 | 能力 |
|------|------|
| **astrbot_plugin_sora** | 明确：`文生视频 <提示词>` / `图生视频 <提示词>+图`；newAPI |
| **video_sora / video_sora2 / bolatuship** | Sora / 柏拉图等中转 |
| **grok_video** | grok2api **图生视频** |
| **comfyui_video** | Comfy **T2V / I2V / R2V** 工作流 |
| **FateTrial_zhipu_video / guijishiping** | 智谱 / 硅基等 |
| **mmx_cli / MiniMax_CLI** | MiniMax 图+视频+音频等 CLI 全家桶 |

### 1.3 本地已装插件

本机 `AstrBot/data/plugins` 下**无**专用生视频插件；仅有电报搬运等与「视频文件转发」相关，**不构成**文生/图生视频参考实现。  
参考实现已浅克隆核对：`/tmp/vid_survey/astrbot_plugin_omnidraw`、`astrbot_plugin_big_banana`。

### 1.4 与旧调研对齐

`PLUGIN_MARKET_SURVEY.md` 曾标视频为 **低优先级 / 不借 Comfy 视频主线**——仍成立于「别做成 Comfy/Sora 专用站」。  
**本次修正**：市场头部**角色化生图插件已普遍内嵌视频**（OmniDraw / big_banana / Agnes / grok_suite），Selfie 若完全不做，会在「多媒体一体」上明显落后；应改为 **可选、后置、独立 video 渠道**，而不是默认无视频。

---

## 2. 行业接入共性（实现时照这个抽象）

几乎所有「中转/OpenAI 兼容视频」都收敛为：

```
POST  {base}/videos/generations   # 或厂商变体
  body: model, prompt, [image/首帧], size/duration/…
  → 同步 mp4 URL  或  task_id

GET   任务查询 / 轮询直到 succeeded
下载 mp4 → 发 Video 组件 / 文件
```

| 维度 | 常见做法 | Selfie 建议 |
|------|----------|-------------|
| 渠道 | **与生图 Provider 分列**（OmniDraw `video_providers` + chain） | `video_channels[]` 独立，不与 image 混超时 |
| 文生视频 | 仅 prompt | `/文生视频` 或 `/视频` 无图 |
| 图生视频 | 1 张首帧为主（banana 明确 max 1）；Omni 可多图参考 | 默认 1 张；多图仅作可选高级 |
| 耗时 | 远大于生图 | **必须**复用现有后台任务表 + 先回执再推送（目标 13） |
| 超时 | 300s+ | 渠道级 timeout，默认高于 image |
| 发送 | `MessageChain` + `Video` / 文件 | 与平台适配层对齐；失败单因提示 |
| 密钥 | 多 key 轮询已有先例 | 复用目标 12 的 key 列表逻辑 |
| 不变量 | 密钥不进仓 | 仅 plugin_data |

**不优先**：Comfy 工作流主线、Sora 专用逆向 Cookie、纯解析类插件。

---

## 3. Selfie 现状与缺口

| 已有（可复用） | 没有 |
|----------------|------|
| 多渠道 image、预检、协议锁、错误分类 | `video_channels` / video adapter |
| 会话模型覆盖、任务查询/取消、后台推送 | `/视频` `/文生视频` `/图生视频` |
| ReferenceCollector、形象参考 | 视频任务结果缓存与 GC |
| Web 试画/监控偏图 | Web 视频试跑与记录筛选 |

定位：**自拍/合影仍是主场**；视频是「同一管理台下的第二条产能」，指令与渠道隔离，避免拖垮生图超时与并发。

---

## 4. 产品方案（指令与体验）

### 4.1 指令（人性化、不混淆）

| 指令 | 行为 |
|------|------|
| `/视频 <描述>` | 无图→文生视频；有图/引用图→图生视频（自动分流，减少两个入口的困惑） |
| `/文生视频 <描述>` | 强制文生（忽略图或提示「将不使用附图」） |
| `/图生视频 <描述>` | 强制要图；无图则单因：「请附图或引用一张图当首帧」 |
| `/生图任务` / `/生图取消` | **扩展为多媒体任务**（文案可改为「出图/视频任务」）或增加 `/视频任务` 别名指向同一任务表 |

可选参数（P1）：`--duration 5`、`--size 720p`（映射渠道能力，未知则忽略并说明）。

### 4.2 与形象/自拍的关系（边界）

- **默认**：视频**不**自动带自拍形象参考（避免「动起来的人设」强绑未经验证的模型）。  
- **可选**：`/视频` + 开关或 `--形象` 时，把形象图当首帧/参考（P1，需模型支持 I2V）。  
- 合影拟人、看镜头等**图片提示词不直接套到视频**；视频用更短的运动描述模板。

### 4.3 帮助

- `/生图帮助` 图卡可后续加一行「视频：/视频」（重做海报时再改，不强制本迭代）。  
- `/生图help` 文字增加视频三小节，与画/自拍分区清晰。

---

## 5. 技术设计（KISS）

### 5.1 配置

```text
video_channels: []          # 结构近似 image_channels：name/provider/base_url/api_key(s)/model/timeout/proxy
enabled_video_model_priority: []
video:
  enable: false             # 总开关；未配置渠道时指令友好提示
  default_duration: 5
  max_concurrent: 1         # 默认比生图更狠地限流
  cache_limit_mb: …         # 可与 image 分桶或共用 image_cache 子目录 video/
```

Web：渠道管理增加「视频渠道」页签（复用现有编辑模态，type=video）。

### 5.2 代码模块（建议）

| 模块 | 职责 |
|------|------|
| `providers_video.py` 或 `providers.py` 内 VideoAdapter | `OpenAICompatibleVideoAdapter`：`/videos/generations` + poll |
| `generator_video.py` 或扩展 fallback | 多 key、不可重试分类复用 `error_classify` |
| `main.py` | 命令 + 可选 `llm_tool generate_video` |
| 任务表 | 现有 `_web_tasks` 增加 `kind: image|video` |

### 5.3 协议优先级（第一期只做一种）

1. **P0**：OpenAI 兼容 `POST /v1/videos/generations`（覆盖大量 newAPI / 中转，对齐 OmniDraw/Sora 插件习惯）  
2. **P1**：智谱 CogVideo 风格（big_banana 已验证）  
3. **P2**：即梦 / Agnes / Grok 厂商特化  
4. **不做默认**：ComfyUI 工作流（独立插件更合适）

### 5.4 任务与发送

1. 预检：渠道 enable、key、url、model（复用 preflight 模式）  
2. 回执任务号 → 后台 `asyncio.create_task`  
3. 轮询/等待 → 下载到 `image_cache/video/` → `event` 发视频  
4. 失败：单因文案（鉴权/超时/内容安全/未开通视频）  
5. 超时创建：**不盲目重 POST**（与 GPT Image 防双扣费同一纪律）

---

## 6. 分期落地

| 阶段 | 内容 | 验收 |
|------|------|------|
| **V0 调研** | 本文 + 更新 MARKET/ROADMAP | 文档入仓 |
| **V1 MVP** | OpenAI 兼容 video 渠道 + `/视频` 自动文生/图生 + 后台任务 + 任务查询 | **已完成 1.3.0**：`video_channels`、`video.py`、`/视频` `/文生视频` `/图生视频`，复用 `/生图任务` |
| **V2** | Web 视频渠道页、试跑、记录 `source=video`；`/文生视频` `/图生视频` 显式命令 | 内嵌/Flask 可配 |
| **V3** | LLM tool、多厂商 adapter、可选形象首帧、时长/分辨率参数 | 工具可调且不阻塞 |
| **Out** | Comfy 主线、解析类视频、默认逆向 Cookie | — |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 视频账单与耗时 | 独立开关、低并发、明确「慢」的进度文案 |
| 平台发视频失败 | 降级文件/链接；单因错误 |
| 模型能力不一 | 渠道能力位：`t2v`/`i2v`；不支持则预检拒绝 |
| 提示词把图规则硬套视频 | 独立短模板 |
| 范围膨胀 | 守自拍主线；视频 P1 队列，不插队未完成的体验债 |

---

## 8. 建议排期（相对 Selfie）

当前图片主线（模型切换、任务、多 key、合影写实、面板）已较完整。  
**下一项工程**：V1 MVP（OpenAI 兼容视频渠道 + `/视频` + 后台任务），工作量约等于「半个生图渠道链路」，优先抄 OmniDraw `VideoManager` 的任务态机与 big_banana 的 I2V 首帧约束。

---

## 9. 参考仓库（本地已扫）

- https://github.com/diaomin66/astrbot_plugin_omnidraw  
- https://github.com/sukafon/astrbot_plugin_big_banana  
- https://github.com/muqing-kg/astrbot_plugin_grok_suite  
- https://github.com/CyreneLian/astrbot_plugin_agnes_image  
- https://github.com/xiaoxi68/astrbot_plugin_jimengapi  
- https://github.com/CCYellowStar2/astrbot_plugin_sora  
- https://github.com/LGinC/astrbot_plugin_comfyui_video  
- https://github.com/lixin0229/astrbot_plugin_grok_video  

（克隆缓存：`/tmp/vid_survey/`，勿提交密钥与大缓存。）
