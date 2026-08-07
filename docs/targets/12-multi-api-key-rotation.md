# 目标 12：渠道多 API Key 轮询

## 来源
- 通用生图、piexian、OpenRouter Gemini 插件
- 调研：`docs/PLUGIN_MARKET_SURVEY.md` B4

## 目标
单渠道支持 `api_keys: []`；429/401/网络失败时切换下一 key 再试（在可重试策略内）。

## 范围
- 配置兼容：单 `api_key` 字符串仍可用。
- Web 渠道编辑可多行 keys（脱敏显示）。
- 与目标 09 不可重试分类配合：鉴权全挂则单因失败。

## 非目标
- 不做跨渠道复杂调度器。

## 验收
- 单测：第一 key 401，第二 key 成功。
- 日志不打印完整 key。

## 状态
待开始
