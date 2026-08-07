# 目标 10：GPT Image 中转双档案与防重复扣费

## 来源
- 市场/仓库：`starmiaoa/astrbot-plugin-gpt-image`
- 调研：`docs/PLUGIN_MARKET_SURVEY.md` §B/B1/B2

## 目标
提升 NewAPI/逆向/官方 GPT Image 兼容性：参数档案可切换；**images generations/edits 的 POST 默认不因超时自动重试**，避免上游已成功仍二次扣费而 Web 仍 running。

## 范围
- standard vs flexible（或等价）请求字段；失败因「参数不支持」时换档一次。
- generator：区分「可安全重试的 GET/下载」与「不可盲目重试的创建类 POST」。
- 文档说明 running 15–60s 与超时策略。

## 非目标
- 不支持所有逆向私服奇技。
- 不删除现有 openai adapter。

## 验收
- 单测：参数错误触发一次档案切换；创建超时不二次 POST。
- 手工：gpt-image-2 中转成功只产生一条上游日志对应一次提交。

## 状态
已完成（2026-08-07）：GPT Image standard/flexible 档案；创建超时不自动重提；参数错误才换档。
