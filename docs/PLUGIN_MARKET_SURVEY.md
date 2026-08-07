# AstrBot 生图插件市场调研（源 → 仓库 → 特色）

> 调研日期：2026-08-07  
> 插件源（官方市场）：`https://api.soulter.top/astrbot/plugins` → 302/跟随至  
> `https://cloud.astrbot.app/api/v1/market/plugins.json`（约 1531 条）  
> 辅源：GitHub `gh search`（astrbot + image/生图/画图 等）  
> 方法：市场 JSON 关键词筛选 → 人工剔除「文转图渲染/图床/审核/斗图」噪声 → 拉取头部仓库 `README/metadata/main` 归纳特色  
> 目的：给 Selfie Image **可集成、可验收**的优化方向，而不是空泛路线图

---

## 1. 插件源怎么找

| 步骤 | 做法 |
|------|------|
| 1 | AstrBot 源码 `plugin_service` / CLI：`https://api.soulter.top/astrbot/plugins` |
| 2 | 跟随重定向得到市场清单 `cloud.astrbot.app/.../plugins.json` |
| 3 | 按 `name/desc/tags/repo` 匹配：生图、画图、image、t2i、gemini、gpt-image、comfy、nai、即梦、seedream… |
| 4 | 打开 `repo` 读 README / metadata / 命令注册，区分「真·AI 生图」vs「Markdown 渲图/图床/审核」 |

本机一次快照：市场约 **1531** 插件；关键词粗筛约 **95**；其中真·AI 生图/改图可用样本约 **25–35**（其余为文转图、图床、斗图、审核、游戏截图等）。

---

## 2. 真·生图插件分型（市场）

### A. 通用多供应商 / 网关（最值得借工程）

| 插件 | Stars(市场) | 仓库 | 一句话特色 |
|------|-------------|------|------------|
| 万象画卷 OmniDraw | 64 | [diaomin66/astrbot_plugin_omnidraw](https://github.com/diaomin66/astrbot_plugin_omnidraw) | **文生图+改图+人设自拍+视频**；多模型热切换；WebUI；预设/人设；最接近 Selfie 产品形态的竞品 |
| 通用生图 | 24 | [Railgun19457/astrbot_plugin_image_generation](https://github.com/Railgun19457/astrbot_plugin_image_generation) | **多 adapter 中台**；任务队列/取消；生图模型切换；LLM 工具；审核；插件公共 API；内嵌 dashboard |
| 图像网关 | — | [Lan-0v0/astrbot_plugin_image_gateway](https://github.com/Lan-0v0/astrbot_plugin_image_gateway) | 国内外多 API + **ComfyUI/A1111 工作流**；优先级回退；条目专属指令；审核参数链 |
| 融合绘图 ronghedraw | 5 | wangyingxuan383-ai/… | 第三渠道聚合 |

### B. 单厂商 / 单协议做深

| 插件 | Stars | 仓库 | 特色 |
|------|-------|------|------|
| Gemini 图像生成 | 51 | [piexian/astrbot_plugin_gemini_image_generation](https://github.com/piexian/astrbot_plugin_gemini_image_generation) | 多供应商模板+优先级；**头像/引用/合并转发**参考图；手办化/表情包切分；LLM 任务号 |
| GPT Image | — | [starmiaoa/astrbot-plugin-gpt-image](https://github.com/starmiaoa/astrbot-plugin-gpt-image) | 专吃 GPT Image / 中转 / 逆向；**standard/flexible 双参数档案自动重试**；**生成 POST 不自动重试防重复扣费**；`--ratio/--resolution` |
| Agnes 图像与视频 | 5 | [CyreneLian/astrbot_plugin_agnes_image](https://github.com/CyreneLian/astrbot_plugin_agnes_image) | Agnes 原生；视频异步轮询；**URL 直发 / 大文件流式上传**；分辨率档位约束 |
| 火山 Seedream | 3 | [MarcoHuanxing/astrbot_plugin_seedream_image](https://github.com/MarcoHuanxing/astrbot_plugin_seedream_image) | 方舟 Seedream 专精 |
| NAI 生图 | 13(GH) | [woakato/astrbot_plugin_nai_image](https://github.com/woakato/astrbot_plugin_nai_image) | NovelAI；**丰富 CLI 参数**；配额/连通性指令；风格/步数/CFG |
| OpenRouter Gemini | 25 | miaoxutao123/…openrouter | 多 key 轮换；免费档；手办化 |
| 免费谷歌逆向 | 68 | cube-lover/gemini | Flow2API 兼容；模型列表切换 |
| 豆包即梦 | 15 | cube-lover/doubao | db/jm 指令；cookie；手办化 |
| Gitee AI 图 | 79 | muyouzhi6/astrbot_plugin_gitee_aiimg | Gitee 图系 |
| Pollinations | 5 | qa296/… | 免费/无 key 向 |
| 即梦 API | 6 | xiaoxi68/jimengapi | 即梦 |
| OpenAI 兼容轻量 | — | zlinwzx147258/openai_image 等 | 文生图/图生图 + LLM tool |

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
| 多供应商 + 优先级回退 | 通用生图、网关、piexian、OmniDraw | 有渠道+优先级，协议推断不稳 | **高**：协议锁定、能力开关 |
| `/生图` `/改图` 拆分 | piexian、starmiao、agnes、网关 | 有画/文生图/图生图/自拍… | 中：可对齐别名，不必改名 |
| `/生图模型` 热切换 | 通用生图、OmniDraw、cube_gemini | 主要靠 Web | **高** |
| `/生图任务` `/取消` | 通用生图、piexian 任务号 | Web task 有，命令弱 | **高** |
| 参考图：消息/引用/转发/@头像 | piexian、通用生图、OmniDraw、豆包 | 有部分，合影/形象强 | **高**：统一收集器 |
| 人设自拍 / 多人设切换 | **OmniDraw**、通用生图人设 | **Selfie 主场**（形象/合影/腿） | 保持差异化，借「多人设切换」体验 |
| 内嵌 Dashboard | 通用生图、OmniDraw WebUI | 已有 embed | 中：打磨测试闭环 |
| GPT Image 双档案 / 防重复扣费 | starmiao | 单路径；fallback 可能重试 POST | **高** |
| 大图发送策略 URL/流式 | agnes | 偏本地缓存发 | 中（QQ 大图超时场景） |
| 多 key 轮询 | openrouter、piexian、通用生图 | 单 key/渠道 | **高** |
| CLI 参数 `--ratio` 等 | starmiao、NAI | 有 `--ar`/`--resolution` 部分 | 中：统一参数语法 |
| 工作流 Comfy/A1111 | 网关、Comfy 系 | 无 | 低（非 Selfie 主线，可选 P3） |
| 视频 | OmniDraw、Agnes | 无 | 低（单独立项） |
| 连通性/配额指令 | NAI `/imgstatus` `/quota` | Web 测试为主 | 中 |
| 手办化彩蛋 | 多插件 | 可用预设，非一等公民 | 低–中：预设即可 |

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

### 5.2 指令（优先集成）

| ID | 集成来源 | 做什么 | 验收 |
|----|----------|--------|------|
| C1 | 通用生图、OmniDraw | `/生图模型` 列表+切换（会话覆盖优先） | 不靠 Web 也能换模 |
| C2 | 通用生图 | `/生图任务` `/生图取消` 绑定现有 web/后台任务或会话任务表 | 长任务可查可停 |
| C3 | starmiao、NAI | 统一可选参数：`--ar`/`--ratio`、`--resolution`、数量 | 帮助文档一致 |
| C4 | piexian | `/改图` 作为图生图一等别名（可映射现有图生图） | 用户心智对齐市场 |
| C5 | NAI | `/生图状态` 或复用 Web 诊断：连通性单因 | 代理/401/空模型可区分 |
| C6 | 守住 Selfie | 合影拟人、晒腿、形象指令不删；仅降敏/稳定性 | 回归提示词用例 |

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
- 部分仓库 clone/raw 超时，特色以 README/metadata 为准，未逐行审计实现。  
- 未把 18+ / 涩图类插件列入集成来源。  
- 密钥与私有中转地址不得写入本仓库。

---

## 8. 结论（给开发的一句话）

Selfie 应继续做 **「角色自拍工作台」**，向市场头部借的是：  
**OmniDraw 的人设/切换体验、通用生图的任务与模型指令与 adapter 纪律、starmiao 的 GPT Image 中转与防重复扣费、piexian 的参考图收集**——而不是再实现一个全能网关或 Comfy 套件。
