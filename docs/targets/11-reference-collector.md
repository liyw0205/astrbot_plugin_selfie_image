# 目标 11：统一参考图收集器 ReferenceCollector

## 来源
- `piexian/astrbot_plugin_gemini_image_generation`
- `Railgun19457/astrbot_plugin_image_generation`
- `diaomin66/astrbot_plugin_omnidraw`
- 调研：`docs/PLUGIN_MARKET_SURVEY.md` B6

## 目标
统一从事件中收集：当前消息图、引用/回复图、合并转发、@ 用户头像、自拍形象图、合影对象图；供画/图生图/自拍/合影共用，减少「有图却没带上」。

## 范围
- 抽 `ReferenceCollector`（或等价模块）；命令与 LLM 工具共用。
- 合影：对象图 vs 形象图角色不混。
- 大小/类型限制与现有 max_image 配置对齐。

## 非目标
- 不实现表情包智能切分整套（piexian 可后续可选）。

## 验收
- 单测覆盖：纯文本无图、单图、引用图、多图角色。
- 合影+风景参考仍走拟人规则（目标不破坏 persona）。

## 状态
已完成（2026-08-07）：`reference_collector.py` 统一收集消息/引用/转发/@头像/上下文/人设；合影对象与人设角色分离；命令路径接入。
