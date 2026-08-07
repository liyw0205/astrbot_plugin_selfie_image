# AstrBot 生图插件市场调研（源 → 仓库 → 特色）

> 调研日期：2026-08-07  
> 插件源（官方市场）：`https://api.soulter.top/astrbot/plugins` → 302/跟随至  
> `https://cloud.astrbot.app/api/v1/market/plugins.json`（约 1531 条）  
> 辅源：GitHub `gh search`（astrbot + image/生图/画图 等）  
> 方法：**描述/标签优先**（不能只靠插件名含 image/生图）→ 再对照 name/repo → 剔除噪声 → 拉头部 README 归纳  
> 目的：给 Selfie Image **可集成、可验收**的优化方向，而不是空泛路线图

---

## 1. 插件源怎么找

| 步骤 | 做法 |
|------|------|
| 1 | AstrBot 源码 `plugin_service` / CLI：`https://api.soulter.top/astrbot/plugins` |
| 2 | 跟随重定向得到市场清单 `cloud.astrbot.app/.../plugins.json` |
| 3 | **先扫 `description` + `tags`**，再扫 `name`/`repository`。关键词含：文生图、图生图、改图、AI绘画、图像生成、手办化、即梦、nano-banana、生视频+图… |
| 4 | **禁止只按插件名过滤**：大量真·生图插件名不含「生图/image」（见 §1.1） |
| 5 | 打开 `repository` 读 README / metadata / 命令，区分真·AI 生图 vs 渲图/图床/审核/斗图 |

本机一次快照：市场约 **1530** 插件；**描述优先**粗筛约 **83**；其中名字完全不像生图但描述是生图的约 **30+**（NAME-MISS）；真·可集成头部仍约 **25–40**。

### 1.1 名字不像生图、描述才是（补漏核心）

| Stars | 市场名 | 描述要点 | 仓库 |
|------:|--------|----------|------|
| 167 | `astrbot_plugin_lmarena` | 对接 LMArena；nano-banana **手办化** | Zhalslar/astrbot_plugin_lmarena |
| 72 | `astrbot_plugin_shoubanhua` | 名「手办化」；**OpenAI 绘图 + 原生 Gemini 文/图生图**，LLM 判断 | shskjw/astrbot_plugin_shoubanhua |
| 68 | `astrbot_plugin_gemini` | 名仅 gemini；**免费谷歌逆向生图** | cube-lover/gemini |
| 33 | `astrbot_plugin_big_banana` | 香蕉梗名；Gemini/OpenAI/Agnes/Vertex **图+视频** | sukafon/astrbot_plugin_big_banana |
| 15 | `astrbot_plugin_doubao` | 名 doubao；**豆包即梦** 文/图生图、手办化 | cube-lover/doubao |
| 15 | `astrbot_plugin_figurine_workshop` | 手办工坊；**Gemini 手办化** | zgojin/… |
| 11 | `astrbot_plugin_grok_suite` | suite 全能；**文生图/图生图/图生视频** | muqing-kg/astrbot_plugin_grok_suite |
| 11 | `astrbot_plugin_ppnai` | 泡泡画图；**NovelAI 官方**文/图、队列额度 | dafeiwu666/astrbot_plugin_ppnai |
| 10 | `astrbot_plugin_bananic_ninjutsu` | 🍌 nano banana 变量参数 | bylkuse/… |
| 10 | `astrbot_plugin_gemini_artist` | artist；给非生图模型挂 **Gemini 生图 tool** | nichinichisou0609/… |
| 8 | `astrbot_plugin_models_ai` | models_ai；**多家模型生成图片** | xiaomizhoubaobei/… |
| 7 | `astrbot_plugin_mmx_cli_tool` | mmx-cli；MiniMax **图像生成**等 | piexian/… |
| 6 | `astrbot_plugin_yoimg` | yoimg；Gitee 图系 + **WebUI 面板** + 人设/润色 | WangYi82909/… |
| 5 | `astrbot_plugin_anima_master` | anima；本地 Comfy **生图** | YayiMiko/anima-master |
| 5 | `astrbot_plugin_txsc` | 缩写名；**通义万相文生图** | zhuiye8/… |
| 5 | `AstrBot_Plugins_Canvas` | Canvas；Gemini **画图改图** | zgojin/… |
| 4 | `Volcengine-text-to-engine` | 名像引擎；**火山豆包生图** | Wayzinx/… |
| 3 | `astrbot_plugin_for_sd_webui` | for_sd；本地 SD `/draw` | xiewoc/… |
| 2 | `liblibApi` | liblib；SD/Flux/Comfy + LoRA | machinad/… |
| 1 | `astrbot_plugin_eidolon` | eidolon；**Seedream 群聊文生图** | YamaArashiHZ/… |
| 0 | `astrbot_plugin_ai_gen` | ai_gen；GPT+生图中转 | Treetore/… |
| 0 | `art_journal_notonly_mais` | 绘卷；**Comfy 可视化** | TheEndZM/… |

**扫描规则（写进后续自动化）**

```text
primary = description + tags
secondary = name + repository
must_include ≈ 文生图|图生图|改图|生图|AI绘画|图像生成|手办化|即梦|nano-banana|images/generations|comfy…
exclude_unless_strong ≈ 文转图|图床|斗图|图片审核|markdown转图|热搜出图|系统状态图
```

---

## 2. 真·生图插件分型（市场，含补漏）

### A. 通用多供应商 / 网关（最值得借工程）

| 插件 | Stars(市场) | 仓库 | 一句话特色 |
|------|-------------|------|------------|
| 万象画卷 OmniDraw | 64 | [diaomin66/astrbot_plugin_omnidraw](https://github.com/diaomin66/astrbot_plugin_omnidraw) | **文生图+改图+人设自拍+视频**；多模型热切换；WebUI；预设/人设；最接近 Selfie 产品形态的竞品 |
| 手办化 shoubanhua | 72 | [shskjw/astrbot_plugin_shoubanhua](https://github.com/shskjw/astrbot_plugin_shoubanhua) | **名不含生图**；OpenAI 绘图格式 + 原生 Gemini 缝合；文/图生图；LLM 智能判断 |
| 大香蕉 big_banana | 33 | [sukafon/astrbot_plugin_big_banana](https://github.com/sukafon/astrbot_plugin_big_banana) | 多提供商图+视频；高级参数（名是梗不是 image） |
| 通用生图 | 24 | [Railgun19457/astrbot_plugin_image_generation](https://github.com/Railgun19457/astrbot_plugin_image_generation) | **多 adapter 中台**；任务队列/取消；生图模型切换；LLM 工具；审核；插件公共 API；内嵌 dashboard |
| 图像网关 | — | [Lan-0v0/astrbot_plugin_image_gateway](https://github.com/Lan-0v0/astrbot_plugin_image_gateway) | 国内外多 API + **ComfyUI/A1111 工作流**；优先级回退；条目专属指令；审核参数链 |
| models_ai / ronghedraw / yoimg | 5–8 | 各仓库 | 多模聚合或 Web 面板润色 |
| 融合绘图 ronghedraw | 5 | wangyingxuan383-ai/… | 第三渠道聚合 |

### B. 单厂商 / 单协议 / 玩法入口做深

| 插件 | Stars | 仓库 | 特色 |
|------|-------|------|------|
| LMArena | 167 | Zhalslar/astrbot_plugin_lmarena | **市场高星**；竞技场免费模；nano-banana 手办化（名无 image） |
| Gitee AI 图 | 79 | muyouzhi6/astrbot_plugin_gitee_aiimg | Gitee；**LLM 单图/批量后台**；生图不阻塞对话 |
| 免费谷歌逆向 | 68 | cube-lover/gemini | 名仅 gemini；Flow2API；模型列表切换 |
| Gemini 图像生成 | 51 | piexian/…gemini_image_generation | 多供应商；头像/转发参考；表情包切分；任务号 |
| GPT Image | — | starmiaoa/astrbot-plugin-gpt-image | 双参数档案；**POST 不自动重试防双扣费** |
| OpenRouter Gemini | 25 | miaoxutao123/… | 多 key；免费档 |
| 豆包即梦 | 15 | cube-lover/doubao | 名 doubao；cookie；手办化 |
| Grok Suite | 11 | muqing-kg/… | 名 suite；文/图/视频一体 |
| 泡泡画图 PPN AI | 11 | dafeiwu666/…ppnai | NovelAI 官方；队列与额度 |
| Agnes | 5 | CyreneLian/… | 图+视频；大图发送策略 |
| 即梦 / Seedream / 通义 / Flux / Pollinations / NAI… | 各 | 各 | 单厂商深做 |
| OpenAI 兼容轻量 | — | openai_image / ai_gen / eidolon 等 | 指令或 LLM tool |

### C. 本地工作流

| 插件 | 特色 |
|------|------|
| ComfyUI_pro / ComfyUI_promax / hub | 工作流 JSON、文生/图生双入口运行时改写 |
| SDGen / A1111 / inkfusion | WebUI 本地 |
| anima-master | 本地 ComfyUI Anima 风格 |

### D. 玩法向（可借「指令爽感」，不借整站架构）

- 手办化、手办工坊、手办化命令（多插件标配彩蛋）
- 手办/表情包切分（piexian SmartMemeSplitter）
- 涩图/lolicon 等（与 Selfie 定位无关，不集成）

### E. 明确排除（避免误当生图能力）

Markdown/回复转图片、图床、图片外显、斗图 hub、图片审核/guard、图库收集、数学函数图、链接转图等。

---

## 3. 特色能力地图（跨插件高频 → Selfie 是否已有）

| 能力 | 代表插件 | Selfie 现状 | 集成价值 |
|------|----------|-------------|----------|
| 多供应商 + 优先级回退 | 通用生图、网关、piexian、OmniDraw、**shoubanhua**、**big_banana** | 有渠道+优先级，协议推断不稳 | **高**：协议锁定、能力开关 |
| 后台生图不阻塞对话 | **Gitee aiimg**、通用生图任务、piexian 任务号 | Web 有后台 task；命令侧弱 | **高** |
| `/生图` `/改图` 拆分 | piexian、starmiao、agnes、网关 | 有画/文生图/图生图/自拍… | 中：可对齐别名 |
| `/生图模型` 热切换 | 通用生图、OmniDraw、cube_gemini | 主要靠 Web | **高** |
| `/生图任务` `/取消` | 通用生图、piexian 任务号 | Web task 有，命令弱 | **高** |
| 参考图：消息/引用/转发/@头像 | piexian、通用生图、OmniDraw、豆包 | 有部分，合影/形象强 | **高**：统一收集器 |
| 人设自拍 / 多人设切换 | **OmniDraw**、通用生图人设、yoimg 提人设 | **Selfie 主场** | 守住；可借切换体验 |
| 内嵌 Dashboard / Web 面板 | 通用生图、OmniDraw、**yoimg** | 已有 embed | 中：打磨测试闭环 |
| GPT Image 双档案 / 防重复扣费 | starmiao | 单路径；fallback 可能重试 POST | **高** |
| 免费/竞技场/香蕉模入口 | **lmarena**、openrouter、bananic | 无 | 低：可选预设，不绑死 |
| 大图发送策略 URL/流式 | agnes | 偏本地缓存发 | 中 |
| 多 key 轮询 | openrouter、piexian、通用生图 | 单 key/渠道 | **高** |
| CLI 参数 `--ratio` 等 | starmiao、NAI、ppnai | 有部分 | 中 |
| 工作流 Comfy/A1111 | 网关、Comfy、anima、liblib | 无 | 低 |
| 视频 | OmniDraw、Agnes、grok_suite、sora | 无 | 低 |
| 连通性/配额 | NAI、ppnai | Web 测试为主 | 中 |
| 手办化彩蛋 | lmarena/shoubanhua/figurine/多插件 | 可用预设 | 低–中：预设即可 |

---

## 4. Selfie 相对市场的真实位置

**已领先 / 应守住**

1. 角色化指令深度：自拍、合影（背景拟人）、看看腿、形象日更  
2. 独立 Flask + AstrBot 内嵌双管理入口、渠道监控  
3. 配置与 AstrBot 原生 `web.*` 分层（不被巨型 `_conf_schema` 绑架）

**明显落后于头部生图插件**

1. 会话内 **模型切换 / 任务查询取消**（通用生图、OmniDraw 标配）  
2. **参考图收集**的完备性（转发、头像、多图角色）  
3. **GPT Image / 中转**兼容（双参数档案、生成请求不盲目重试）  
4. **多 key**、失败分类（不可重试词典）  
5. 与 OmniDraw 比：多人设切换、视频、副脑提示词（后两项非必须）

**不应做的**

- 做成第二个「通用生图」而丢掉自拍主线  
- 为追 stars 去接 Comfy/视频/手办整站  
- 把市场里逆向 cookie 方案当默认依赖

---

## 5. 集成方案（有来源、可验收）

### 5.1 后端（优先集成）

| ID | 集成来源 | 做什么 | 验收 |
|----|----------|--------|------|
| B1 | starmiao GPT Image | 生成/编辑 **POST 默认不自动重试**（防 NewAPI 双扣费）；网络层与业务层重试分离 | 超时失败不二次提交；单测 |
| B2 | starmiao | **参数档案**：standard（官方 size）vs flexible（ratio 中转）失败换档一次 | 中转 gpt-image-2 成功率↑ |
| B3 | 通用生图 / 网关 | **协议锁定** + openai / openai_chat 分流；模型能力开关（文生/图生/比例） | 名称含 gemini 的中转不再打原生 Google |
| B4 | 通用生图 / openrouter | 渠道 **api_keys[] 轮询** | 401/429 切下一 key |
| B5 | 通用生图 / 网关 | **不可重试错误分类**（鉴权/模型不存在/内容安全） | 不空转打满 timeout |
| B6 | piexian / 通用生图 | **统一 ReferenceCollector**（消息图、引用、转发、@头像、形象图、合影对象） | 合影/改图少「没图」 |
| B7 | agnes（可选） | 大图发送：优先 URL/分块，失败回落文件 | QQ 大图少超时 |
| B8 | Gitee aiimg / 通用生图 | **生图任务与对话解耦**（后台跑完再推/可查） | 群聊不因生图卡死 |

### 5.2 指令（优先集成）

| ID | 集成来源 | 做什么 | 验收 |
|----|----------|--------|------|
| C1 | 通用生图、OmniDraw | `/生图模型` 列表+切换（会话覆盖优先） | 不靠 Web 也能换模 |
| C2 | 通用生图、Gitee aiimg | `/生图任务` `/生图取消`；强调后台不阻塞 | 长任务可查可停 |
| C3 | starmiao、NAI、ppnai | 统一可选参数：`--ar`/`--ratio`、`--resolution`、数量 | 帮助文档一致 |
| C4 | piexian、shoubanhua | `/改图` 一等别名；可选「手办化」预设而非新架构 | 心智对齐市场 |
| C5 | NAI、ppnai | `/生图状态` 连通性/额度单因 | 代理/401/空模型可区分 |
| C6 | 守住 Selfie | 合影拟人、晒腿、形象指令不删 | 回归提示词用例 |
| C7 | lmarena/bananic（可选） | 文档层记录「香蕉/竞技场」类渠道配置样例，不写死依赖 | 用户可选用 |

### 5.3 前端（优先集成）

| ID | 集成来源 | 做什么 | 验收 |
|----|----------|--------|------|
| F1 | 通用生图 dashboard + 电报 bridge | 内嵌免 token、测试任务闭环（已部分完成→收口测） | NewAPI 成功⇒有图+监控 |
| F2 | OmniDraw WebUI 体验 | 模型/人设「当前生效」状态条；缓存模型 vs 已启用 | 不误测未启用模型 |
| F3 | 通用生图供应商模板 | 保存/测试 **预检**（缺 key/url/模型） | 无静默失败 |
| F4 | piexian 供应商表 | 渠道卡片显示协议、能力、proxy 是否配置 | 网络类站可发现需 proxy |
| F5 | starmiao 文档 | 测试页说明「生成中 15–60s≠失败」 | 减少误判 |

---

## 6. 与旧路线图关系

- 旧 `OPTIMIZATION_ROADMAP.md` 的自动迭代、审查清单、目标队列机制 **保留**。  
- **优先级与条目以本调研的 B/C/F 表为准**（有插件来源），避免「拍脑袋优化」。  
- 目标文档建议映射：  
  - 06 ← F1/F5  
  - 07 ← B3  
  - 08 ← C1/C2  
  - 09 ← B5  
  - 新增 10 ← B1/B2（GPT Image 计费安全与双档案）  
  - 新增 11 ← B6（ReferenceCollector）  
  - 新增 12 ← B4（多 key）

---

## 7. 证据与局限

- 市场 JSON 字段含 name/description/repository/stars/tags；stars 为市场侧数据，可能与 GitHub 不完全一致。  
- **描述优先**后仍会混入边缘插件（热搜出图、对称镜像、水印、mermaid）；集成前必须读 README 再定「真·生图」。  
- 部分仓库 clone/raw 超时，特色以 README/metadata 为准，未逐行审计实现。  
- 未把 18+ / 涩图类插件列入集成来源。  
- 密钥与私有中转地址不得写入本仓库。

---

## 8. 结论（给开发的一句话）

扫市场时 **先读 description/tags，再看 name**，否则会漏掉 lmarena、手办化、gemini 逆向、doubao、grok_suite、big_banana 等头部。  
Selfie 仍做 **「角色自拍工作台」**，向市场借：  
**OmniDraw 人设/切换、通用生图任务与 adapter 纪律、starmiao GPT Image 防双扣费、piexian 参考图收集、Gitee/任务不阻塞对话、shoubanhua 类双协议缝合经验**——而不是改名追梗或做成 Comfy 全能网关。
