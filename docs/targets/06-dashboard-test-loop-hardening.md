# 目标 06：内嵌 Dashboard 与渠道测试闭环 hardening

## 目标
把已落地的内嵌免 Token、bridge 启动顺序、safeStorage、任务轮询重试与 OpenAI 收包快路径固化为可回归验收，并补齐仍缺的前端契约测试，避免「NewAPI 成功但网页不刷新 / 监控为空 / 内嵌假登录墙」回潮。

## 范围
- 文档化并锁定：bridge 预加载、`/api/x`→`x` endpoint、safeStorage、轮询 failStreak。
- 任务终态：`running` 必达 `succeeded|failed`；成功必写 generation record。
- 渠道测试默认稳健参数（增强默认关、比例 1:1 倾向）保持或可配置说明。
- 为 web/dashboard 关键路径增加不依赖真实 Provider 的契约测试或 ad-hoc 脚本。

## 非目标
- 不重做整站 UI 视觉。
- 不改为 React/Vue 工程。
- 不在本目标移植新 Provider。

## 验收
- 内嵌页不出现必须 Web Token 才能进入的登录墙（Dashboard 已登录前提下）。
- 模拟/真实成功响应后 task 非永久 `running`，监控可见记录。
- `py_compile` + 相关 unittest/ad-hoc 通过；无密钥入仓。

## 验证命令
```bash
python -m py_compile web.py dashboard_api.py main.py providers.py provider_parser.py
python -m unittest tests/test_core.py
# 可选：本地 Flask health + 任务提交轮询（密钥仅环境变量）
```

## 风险与回滚
- 风险：改 bridge 路径再次与父页约定不一致。
- 回滚：恢复 endpoint 剥 `/api/` 约定与 bridge 预加载顺序。

## 状态
进行中（代码已部分落地，本目标负责收口验收与防回潮）
