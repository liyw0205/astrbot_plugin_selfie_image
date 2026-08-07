# 目标 07：渠道协议锁定与 openai / openai_chat 分流

## 目标
避免 NewAPI 等 OpenAI 兼容中转因模型名被推断成 gemini/z_image 等原生协议而失败；明确 images API 与 chat 出图两条路径。

## 范围
- 渠道级 `protocol_lock` 或等价：默认沿用 `provider_type`，仅显式 `model_provider_types` 可覆盖。
- `openai`：`/v1/images/generations|edits`；`openai_chat`（可别名 gemini_openai）：chat 多模态出图。
- 文档与 Web 下拉说明；单测覆盖推断不再跨协议乱跳。

## 非目标
- 不一次移植全部参考仓 adapter。
- 不强制所有站开启 stream。

## 验收
- 同中转下 `gpt-image-*` 与 `gemini-*` 名称在 lock 开启时均走渠道声明协议。
- 错误信息能区分协议错误与上游 401/503。
- unittest 覆盖 resolve 逻辑。

## 验证命令
```bash
python -m unittest tests/test_core.py -k protocol -k provider
python -m py_compile models.py providers.py main.py
```

## 风险与回滚
- 风险：依赖「按模型名自动选协议」的旧配置行为变化。
- 回滚：默认 lock=false 或迁移时写回显式 model_provider_types。

## 状态
待开始
