# 目标 01：统一 Provider 结果解释

## 目标
将 Provider 成功响应中的图片提取、下载失败诊断和敏感信息脱敏收敛到 `BaseImageAdapter`，避免各 adapter 对同一响应重复实现并产生不一致的错误信息。

## 范围
- 统一 OpenAI、Gemini、Gemini OpenAI、简单 OpenAI 与 Agnes adapter 的结果转换入口。
- 保留 Gemini OpenAI 和 Agnes 当前更详细的失败诊断。
- 为成功、链接下载失败和 base64 解码失败补回归测试。

## 非目标
- 不改变请求 URL、请求载荷、超时、代理或 fallback 策略。
- 不新增 Provider 类型。

## 验收
- 所有 adapter 经统一入口生成 `ImageGenerateResult`。
- 失败预览不会泄露密钥或认证头。
- `python -m unittest tests/test_core.py`、`py_compile` 和 `git diff --check` 通过。
