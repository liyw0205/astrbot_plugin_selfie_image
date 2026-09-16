# 空实现与重复实现专项优化计划

> 更新日期：2026-09-16。本文基于当前工作区静态审计结果重写，并已按阶段完成空实现治理与重复实现收敛。完成状态以实际代码和测试结果为准。

## 1. 审计范围与基线

本次检查覆盖仓库内全部 Python 文件，并重点核对以下模块：

- 入口与任务：`main.py`、`tasks/task_manager.py`、`tasks/task_views.py`；
- 自拍与 COS：`features/persona.py`、`cos/cos_looks.py`、`cos/leg_focus.py`；
- 配置与通用工具：`core/models.py`、`core/utils.py`；
- 两套 Web 接口：`webui/web.py`、`webui/dashboard_api.py`、`webui/services.py`；
- 运维脚本：`scripts/douyin_frames.py`、`scripts/video_prompt_frames.py`；
- 测试代码中的 fake、stub、空回调只用于辅助判断，不作为生产空实现缺陷处理。

当前工作区已有多处未提交业务和测试改动。本计划不得要求回退、覆盖或顺带整理这些改动；实施每一项前都要重新读取目标文件并以当时内容为准。

固定验证命令：

```bash
PYTHONPATH=.. .venv/bin/pytest -q
.venv/bin/python -m compileall -q core features generation tasks webui prompts studio cos main.py
.venv/bin/python scripts/check_noop_implementations.py
.venv/bin/python scripts/check_dashboard_fallback.py
git diff --check
```

只有修改 `pages/dashboard/index.html` 时才运行 `scripts/generate_dashboard_fallback.py`；不得用生成动作掩盖无关的 fallback 差异。

## 2. 空实现审计结论

### 2.1 当前没有生产业务空实现

AST 检查最初得到两处兼容日志桩；媒体脚本合并后只保留一处：

- `scripts/video_prompt_frames.py:48` 的 `Logger.debug()`，其余日志级别复用该方法。

该方法是加载外部 NoneBot 媒体解析器时使用的 no-op 日志兼容桩，不承载插件业务。它不应被补成真实业务逻辑，也不应为了消除 `return None` 而输出噪声。

以下命中同样不是空实现：

- `core/providers.py:221` 的 `BaseImageAdapter.generate()` 抛出 `NotImplementedError`，是明确的适配器契约；
- `except ...: pass` 主要用于临时文件清理、可选依赖降级、兼容旧返回值或尽力而为的通知，不等价于未实现函数；
- 测试中的空构造器、空发送回调、空 sleep 和上下文管理器是 test double。

### 2.2 空实现治理（已完成）

已新增 `core/noop_check.py` 和 `scripts/check_noop_implementations.py`，并通过 pytest 覆盖正常仓库与临时空函数失败场景：

1. 新增 AST 静态检查脚本或 pytest，用允许名单记录上述日志桩以及测试目录。
2. 对生产模块中新出现的纯 `pass`、纯 `...`、纯 `return None` 函数报错。
3. 对 `NotImplementedError` 只允许明确的基类/协议方法，并要求有说明性 docstring。
4. 不禁止有上下文的异常降级；如需收紧，单独审查异常范围、fallback 和日志，不能机械替换所有 `pass`。

验收标准：检查可在本地和 CI 独立运行；当前代码通过；临时加入一个未列入允许名单的空函数时检查失败，并指出文件、行号和函数名。

## 3. 重复实现清单与执行顺序

### P0：统一任务媒体类型判定（已完成）

**代码证据**

同一判定逻辑现有三份：

- `tasks/task_views.py:37`；
- `tasks/task_manager.py:84`；
- `main.py:2491`。

其中 `main.py` 与 `task_views.py` 的函数体完全相同，`task_manager.py` 仅把 `dict` 判断放宽为 `Mapping`。该逻辑参与任务过滤、列表/详情展示、重启恢复、统计、取消校验和图片/视频并发路径。三份实现一旦漂移，同一个持久化任务可能在不同入口被识别成不同媒体类型。

**实施方向**

1. 在 `tasks/task_views.py` 提供一个公开的单一函数，例如 `task_media_type()`，输入统一接受 `Mapping[str, Any]`。
2. `tasks/task_manager.py` 和 `main.py` 直接调用该函数；删除各自函数体。
3. 如为兼容内部调用保留同名方法，只允许一行委托，并在后续确认无外部依赖后再删除。

**测试与验收**

- 参数化覆盖顶层 `media_type`、`request_data.media_type`、`kind=video`、中文 kind、source 含 video、无效/缺失 `request_data`；
- 对同一任务断言列表过滤、详情显示、任务统计和取消入口得到一致分类；
- 保持旧任务默认归类为 `image`。

### P0：统一腿部裁切判定（已完成）

**代码证据**

- `cos/leg_focus.py:298` 已定义 `is_leg_calf_crop_action()`，依据 `CALF_CROP_POSES` 和腿部契约标记判断；
- `features/persona.py:27` 又定义一份同名函数，但规则不同：任何 `_crop` pose、`不展示脚部`、`看看腿` 都直接判真；
- `features/persona.py:1100` 实际使用本地版本，同时该模块已经从 `cos/leg_focus.py` 导入其他腿部契约函数。

这不是单纯文本重复，而是同一领域概念存在两套不同规则，后续新增 pose 或标记时会产生提示词构图分叉。

**实施方向**

1. 先用测试把当前两份规则的输入矩阵列清楚，确认哪些差异是历史兼容、哪些是误差。
2. 将最终规则收敛到 `cos/leg_focus.py`；`features/persona.py` 只导入调用。
3. 对需要兼容的自然语言标记显式加入唯一实现，不能在调用层补第二套条件。

**测试与验收**

覆盖 `【pose:*】`、`【legs:outfit】`、`【crop:calves】`、`看看腿`、`下半身穿搭`、`不展示脚部`、普通自拍文本；统一前后的 persona prompt 必须保持已确认的脚部裁切语义。

### P0：统一手机遮脸请求判定（已完成）

**代码证据**

- `cos/cos_looks.py:1940` 与 `features/persona.py:39` 各有一份 `requests_phone_face_cover()`；
- 两份当前词表和函数体相同；
- COS 构图在 `cos/cos_looks.py:2067` 调用该判定，persona 模块保留另一份定义。

**实施方向**

以 `cos/cos_looks.py` 为唯一实现，删除 persona 中的复制体并改为导入。若后续发现模块依赖方向不合适，再把纯文本判定下沉到独立 prompt/intent 工具模块，但本项不顺带搬迁其他 COS 逻辑。

**测试与验收**

参数化覆盖中英文手机词、遮脸词、仅手机、仅遮脸和普通自拍；COS 与 persona 路径必须共享同一结果。

### P1：收敛 Web 任务状态与画廊适配代码（已完成）

**代码证据**

- Flask 入口 `webui/web.py:586`、`:641`、`:1313` 三个任务状态 handler 重复任务号校验、脱敏读取和 404 映射；
- Dashboard 入口 `webui/dashboard_api.py:467` 与 `:1107` 也有完全相同的状态 handler；
- `webui/dashboard_api.py:1117` 与 `:1215` 的两个 gallery handler 函数体完全相同；
- 两套 Web 技术栈需要保留各自认证和响应封装，但领域校验、查询解析、状态码映射不应各自复制。

**实施方向**

1. 先在各自适配器内部抽取最小 helper，统一任务号校验和任务读取，不跨越同步 Flask/异步 Dashboard 的响应边界。
2. Dashboard 的 studio/creative gallery 路由复用一个查询与调用 helper。
3. 只有在两套入口的契约测试齐全后，才把纯领域逻辑继续下沉到 `webui/services.py`。
4. 不合并认证、`jsonify/json_response`、同步/异步执行器或路由注册机制。

**测试与验收**

对 Flask 与 Dashboard 建立同表契约测试，至少覆盖合法任务、非法任务号、任务不存在、敏感字段脱敏、gallery 默认值、非法 limit/offset 和后端异常；保留现有 HTTP 状态与响应 envelope。

### P2：统一保序字符串去重 helper（已完成）

**代码证据**

- `core/models.py:1458` 的 `unique_values()`；
- `core/utils.py:1318` 的 `unique()`；
- 两者函数体完全相同，均执行“转字符串、strip、过滤空值、保序去重”。

**实施方向**

将唯一实现放在 `core/utils.py` 并使用表达语义的公开名称；`core/models.py` 导入复用。先确认不会形成循环导入，再迁移调用点。不要用 `dict.fromkeys()` 直接替代，因为现有函数还承担字符串转换和空值过滤。

**测试与验收**

覆盖空值、空白、非字符串、重复值和输入顺序；运行配置规范化、API key 拆分和图片 URL 提取相关测试。

### P2：合并或删除重复的媒体反推脚本（已完成）

**代码证据**

- `scripts/douyin_frames.py` 与 `scripts/video_prompt_frames.py` 都实现 NoneBot shim、外部 native parser 加载、媒体下载和 15%/50%/85% 三帧抽取；
- `scripts/video_prompt_frames.py` 已包含更完整的直链、大小限制、`.part` 清理、ffmpeg 错误处理和视觉反推流程；
- `.gitignore:15` 已把 `scripts/douyin_frames.py` 标记为本地临时脚本，但该文件当前仍存在于工作区，因此不能假定它是正式交付入口。

**实施方向**

为兼容可能存在的仓库外调用，`douyin_frames.py` 已缩减为对 `video_prompt_frames.py` 的薄包装；下载、解析和抽帧只保留一份实现。

**测试与验收**

至少用本地文件 URL 或 mock 下载验证参数转发、输出目录、三帧时间点和错误退出码；没有外部解析器环境时必须明确跳过对应集成测试，不能伪造成功结果。

## 4. 明确保留的重复边界

以下相似代码暂不合并，除非先补齐契约证据：

1. `webui/web.py` 与 `webui/dashboard_api.py` 的响应封装、认证和路由注册。两者分别服务 Flask 独立页与 AstrBot Dashboard，生命周期和返回 envelope 不同。
2. 图片与视频 provider 的请求、轮询和下载路径。协议、认证、返回格式和 fallback 顺序不同，不能只按代码形状合并。
3. 测试中的 fake class、空回调和局部 runner。只有重复测试已经造成维护错误时才抽 fixture，不能为了降低行数隐藏场景差异。
4. `BaseImageAdapter.generate()` 的抽象契约和外部解析器 logger no-op 桩。
5. `except ...: pass`。是否需要日志或收窄异常必须逐处证明，禁止全局机械替换。

## 5. 分阶段实施

1. 阶段 A：已完成空实现守卫和重复判定参数化测试。
2. 阶段 B：已完成任务媒体类型、腿部裁切、手机遮脸三个 P0 单一实现。
3. 阶段 C：已下沉任务状态与 gallery 纯逻辑，同时保留两种认证和响应协议。
4. 阶段 D：已统一保序去重 helper，并将旧媒体脚本改为兼容包装。
5. 专项回归：`28 passed, 60 deselected`；最终全量验证：`531 passed, 1 skipped, 12 subtests passed`。

## 6. 完成定义

每个候选必须同时满足：

- 有测试固定合并前的有效行为和历史兼容输入；
- 同一领域规则只保留一个真实函数体，其余调用点直接导入或做有期限的薄委托；
- 没有新增循环导入、跨层依赖或不同协议共用错误抽象；
- 命令名、任务持久化字段、任务媒体分类、提示词契约、Web 状态码和响应结构不变；
- 局部测试与第 1 节固定验证命令全部通过；
- `git diff` 只包含本项必要修改，不覆盖用户当前未提交改动；
- 文档中的“已完成”必须有实际代码和测试结果支撑，未实施项目继续保留为待办。
