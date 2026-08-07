# Selfie Image 三向优化方案（前端 / 后端 / 指令）

> 基线：`astrbot_plugin_selfie_image` 1.1.0  
> **市场调研（必读）**：[PLUGIN_MARKET_SURVEY.md](./PLUGIN_MARKET_SURVEY.md)  
> 插件源：`api.soulter.top/astrbot/plugins` → `cloud.astrbot.app/api/v1/market/plugins.json`  
> 主工程参考：通用生图、OmniDraw、piexian、starmiao GPT Image、图像网关；**补漏**：手办化、lmarena、big_banana、doubao、grok_suite、Gitee aiimg（名不一定含「生图」）  
> 原则：**描述/标签优先扫市场**；借头部工程能力，守自拍差异化；每条优化可追溯仓库。

---

## 0. 定位与边界

| 是 | 不是 |
|----|------|
| 命令 + LLM 工具 + Web/内嵌页 | 独立前端工程 |
| 角色化自拍 / 合影 / 形象 | 第二个通用生图或 Comfy 套件 |
| 单因失败、可运营渠道 | 密钥进仓、多可能兜底 |

**不变量**：`web.*` 与 plugin_data 分层；内嵌免 Token；`trust_env=False`+显式 proxy；晒腿等玩法默认保留。

**可借 / 不借**：见调研文档。可借任务指令、协议锁定、GPT Image 防双扣费、参考图收集、多 key、错误分类。不借 Comfy/视频主线、逆向 cookie 默认方案。

---

## 1. 调研驱动优先级

### 后端 B

| 优先级 | ID | 来源 | 项 |
|--------|-----|------|-----|
| P0 | B1/B2 | starmiao GPT Image | 生成 POST 不盲目重试；standard/flexible 双档案 |
| P0 | B3 | 通用生图 / 网关 / **shoubanhua** | 协议锁定 + openai/openai_chat（双协议缝合经验） |
| P0 | B5 | 通用生图 / 网关 | 不可重试错误分类 |
| P0 | B8 | **Gitee aiimg** / 通用生图 | 生图后台化，不阻塞对话 |
| P1 | B6 | piexian / 通用生图 / OmniDraw | ReferenceCollector |
| P1 | B4 | 多头部插件 | 多 api_key 轮询 |
| P2 | B7 | Agnes | 大图发送策略 |

### 指令 C

| 优先级 | ID | 来源 | 项 |
|--------|-----|------|-----|
| P0 | C1 | 通用生图 / OmniDraw | `/生图模型` |
| P0 | C2 | 通用生图 | `/生图任务` `/生图取消` |
| P0 | C6 | Selfie | 合影拟人 + 晒腿保留 |
| P1 | C3 | starmiao / NAI | 统一可选参数 |
| P1 | C4 | piexian 等 | `/改图` 别名 |
| P1 | C5 | NAI | 连通性/状态 |

### 前端 F

| 优先级 | ID | 来源 | 项 |
|--------|-----|------|-----|
| P0 | F1 | 通用生图 + 电报 | 内嵌免 token + 测试闭环 |
| P0 | F3 | 通用生图 | 配置预检 |
| P1 | F2 | OmniDraw | 当前模型/启用 vs 缓存 |
| P1 | F4/F5 | piexian / starmiao | 渠道可见性、长耗时提示 |

---

## 2. 自动后续优化

1. 读 `PLUGIN_MARKET_SURVEY.md` + 本文件 + `docs/targets/*`  
2. 只做队列第一项待办  
3. 实现与 commit 注明**来源插件**  
4. 审查：成功闭环、无双 POST、无密钥、合影/晒腿不误伤  
5. 验证 → push → 目标完成 → 下一项  

映射：06←F1，07←B3，08←C1/C2，09←B5，10←B1/B2，11←B6，12←B4，13←B8。

---

## 3. 近两周

1. 目标 10（GPT Image 防双扣费/双档案）  
2. 09 + 07（错误分类 + 协议锁定；含 shoubanhua 双协议经验）  
3. 08 + 13（模型/任务指令 + 后台不阻塞）  
4. 11（参考图收集）  
5. 03/06（预检与 Dashboard）  
6. 12（多 key）  

完整仓库表、**描述优先补漏表**见 `PLUGIN_MARKET_SURVEY.md`。
