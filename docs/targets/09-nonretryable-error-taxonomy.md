# 目标 09：不可重试错误词典与失败分类

## 目标
参考通用生图：对 401/403/404、model_not_found、unsafe、invalid token 等立即失败，避免空转重试耗时耗额度；对外仍单因提示。

## 范围
- 集中错误分类：可重试 / 不可重试 / 内容安全 / 鉴权 / 模型不存在。
- generator fallback 与 web_test 共用。
- 用户可见文案映射（管理端可看脱敏详情）。

## 非目标
- 不改变成功响应解析。
- 不把多原因拼进用户一句回复。

## 验收
- 注入 401/model_not_found 只尝试一次（在 max_attempts 允许的语义下立即停）。
- 文案无密钥；unittest 覆盖分类。

## 验证命令
```bash
python -m unittest tests/test_core.py -k error -k retry
python -m py_compile generator.py providers.py utils.py
```

## 风险与回滚
- 风险：误把可恢复 429 标成不可重试。
- 回滚：429/5xx 保持可重试。

## 状态
待开始
