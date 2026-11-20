# COS 通用词库

各文件顶层使用 `schema_version: 1` 和以 ID 为键的 `items`。`looks/` 仅存套装正文；动作、摄影、表情通过 ID 在代码中组合，不把来源判断或候选分支交给生图模型。

| 文件 | 内容 | 必填条目字段 |
| --- | --- | --- |
| `_expressions.json` | 面部神态，不指定头部朝向、视线目标、姿势或机位 | `label`、`prompt`、`aliases` |
| `_poses.json` | 身体与手脚动作、必要支撑关系 | `title`、`prompt`、`framing_ids`、`expression_ids`、`random_camera_ids` |
| `_framings.json` | 镜头方向、角度与裁切范围，不另选身体动作 | `title`、`prompt`、`random_camera_ids` |

姿势条目还可使用 `orientation`（身体展示方向）、`pose_family`、`random_weight`、`camera_ids`、`scene_ids`、`incompatible_expression_ids`、`traits`、`explicit_only`。`scene_ids` 限定自动组合可依赖的场景，未命中的自定义场景不会自动抽到这些专用动作；用户明确指定动作时仍尊重原有优先级。请求别名集中在 `_poses.json` 顶层的 `request_aliases`。`framing_ids` 是该动作可用的摄影选项；正面动作可以使用侧前方、轻俯拍等兼容选项，不等于锁定正面机位。

构图的 `camera_ids` 是可选硬限制，未填写时不追加限制；`scene_ids` 是可选场景白名单，未填写时不限制场景。`random_camera_ids` 规定可自动抽取的拍摄方式：`third` 为他拍，`first` 为观察者主观视角，`selfie` 为自摄；空列表表示不进入随机池。`explicit_only: true` 表示仅显式选择。可选字段无需补空值，不同条目的字段数量可以不同。

加载时由 `cos/cos_looks_loader.py` 校验字段类型和跨文件引用。运行时先确定场景和动作，再从兼容列表选择机位、神态与妆造。

普通套装正文只保存服饰和必要的专属排除项，不重复保存“禁止动漫插画、赛璐璐、游戏立绘、3D渲染、塑料皮肤和超现实发光”这类公共媒介约束。聊天指令在 `build_cos_look_action` 中拼接；快速试画和两个画布在解析参考图后通过 `finalize_cos_prompt_for_request` 调用同一个 `normalize_cos_prompt_negatives`：真人模式使用摄影排除项，动漫模式使用动漫排除项，自动且有参考图时沿用参考图媒介。独立完整提示词仍按特殊套装路径执行。

`research/` 保存服饰核查缓存与来源，不参与套装索引或生图提示词加载；服饰细节核实后再写回 `looks/`。

火影忍者的12套成年女性性感私设已接入 `looks/火影忍者.json`，支持“火影忍者 / 火影 / Naruto”及角色名选择；截图款春野樱保留审核通过的短裙与凉鞋补全设计。套装只描述服装与假发，继续共用场景和摄影词库。

蓝紫水纹泳池边、冷白洗手台、灰石卫浴、车内后排逆光已按同一词库规则接入。风格仅改变成像质感，主光由各场景的 `lighting_ids` 决定；池边使用干燥池沿坐姿，卫浴使用有台沿支撑的动作，车内限定后排坐姿与近景。专用构图通过 `scene_ids` 限定环境，不自动加入手机、湿衣或服装替换。镜前卧室使用独立场景，镜面位于人物正前方并与站位对齐，只保留同一人物的一处物理一致倒影；普通自拍仍按设备在画外处理，明确要求对镜时才使用对镜规则。

床面侧卧轻靠只与 `bedroom_sleep`、`gray_bed_recline` 配对，并使用床面近中景；沙发斜倚、门边回望继续复用 `relaxed_recline`、`over_shoulder` 等现有动作，不另建同义姿势。新增动作只调整姿势与场景互动，不改 COS 套装。
