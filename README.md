# AstrBot Selfie Image 生图自拍

当前版本 `1.3.5`，要求 AstrBot `>=4.13.0,<5`。

这是 AstrBot 生图自拍插件，包含生图、参考图图生图、AI 自拍、LLM 工具调用和 Flask Web / 内嵌 Dashboard 管理页。

- `/生图帮助`：只发仓库预生成的帮助图（`assets/help_poster.png`）
- `/生图help`：详细文字指令
- **不会**在运行时再调渠道画帮助图

前端与帮助图方案见 [`docs/UI_AND_HELP_IMAGE_PLAN.md`](docs/UI_AND_HELP_IMAGE_PLAN.md)。

后续优化按三向推进（前端 / 后端 / 指令）。  
- 市场调研（插件源与特色）：[`docs/PLUGIN_MARKET_SURVEY.md`](docs/PLUGIN_MARKET_SURVEY.md)  
- 优化路线图：[`docs/OPTIMIZATION_ROADMAP.md`](docs/OPTIMIZATION_ROADMAP.md)  
- **视频（文生/图生）方案**：[`docs/VIDEO_GENERATION_PLAN.md`](docs/VIDEO_GENERATION_PLAN.md)  
- 前端与帮助图：[`docs/UI_AND_HELP_IMAGE_PLAN.md`](docs/UI_AND_HELP_IMAGE_PLAN.md)

## 使用步骤

1. 将 `astrbot_plugin_selfie_image` 放入 AstrBot 的插件目录并安装依赖：

   ```bash
   pip install -r astrbot_plugin_selfie_image/requirements.txt
   ```

2. 在 AstrBot 插件配置中只设置 Web 入口：

   - `web.enable`
   - `web.host`
   - `web.port`
   - `web.token`

   这些配置只在插件启动时读取。生图渠道、模型、自拍、人设、权限等完整配置请在 Flask Web 面板中保存，文件单独存放在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_selfie_image/selfie_image_config.json`。

3. 打开 Flask Web，在「渠道管理」里添加至少一个可用生图渠道；如需启用提示词审核、出图审核或 OCR / 识图，另行添加审核渠道。

   OpenAI 示例：

   ```json
   {
     "name": "openai",
     "provider_type": "openai",
     "base_url": "https://api.openai.com",
     "api_key": "sk-...",
     "model": "gpt-image-1",
     "enabled_models": ["gpt-image-1"],
     "timeout": 180,
     "enabled": true
   }
   ```

   Agnes Image 2.1 Flash 示例：

   ```json
   {
     "name": "agnes",
     "provider_type": "agnes",
     "base_url": "https://apihub.agnes-ai.com",
     "api_key": "YOUR_API_KEY",
     "model": "agnes-image-2.1-flash",
     "enabled_models": ["agnes-image-2.1-flash"],
     "timeout": 280,
     "enabled": true
   }
   ```

4. 重载插件后使用命令：

   - `/画 一只白猫坐在窗边 --ar 1:1`、`/画 3 一只白猫坐在窗边` 或 `/画 预设名 3 额外提示词`
   - `/文生图 原始提示词 --ar 1:1`（提示词直通，不做增强）
   - `/图生图 原始提示词 --ar 1:1` 并附带/引用图片（提示词直通，不做增强）
   - `/自拍 看着镜头自然自拍 --ar 3:4`、`/自拍 3 看着镜头自然自拍`、`/自拍 预设名 额外提示词` 或 `/看看 看着镜头自然自拍 --ar 3:4`
   - `/看看腿 居家自然一点 --ar 3:4`
   - `/看看你 窗边自然回头 --ar 3:4`
   - `/形象设置` 并附带图片或引用图片
   - `/形象查看`
   - `/形象清除`
   - `/生图帮助`

5. LLM 可调用工具：

   - `generate_image(prompt, count, aspect_ratio, resolution)`
   - `generate_selfie(action, count, aspect_ratio, resolution)`

6. Flask Web 默认地址：

   - `http://127.0.0.1:14514`
   - `web.token` 默认占位值 `changeme` 会在插件启动时自动替换为随机 Token，并在日志中输出。
   - `web.token` 留空时 Web API 不做 Token 校验，前端会自动尝试免 Token 进入。
   - 修改 `web.port`、`web.host` 后需要重载插件让监听端口生效。
   - 配置了 `web.token` 时，打开后在登录页输入该 Token；留空时会自动进入。

7. AstrBot Dashboard 内嵌管理页：

   - 入口：AstrBot WebUI → 插件详情 → 插件行为里的 `dashboard` 页面。
   - 复用 AstrBot Dashboard 登录态，**不需要单独输入 Web Token**，也不依赖独立 Flask 端口。
   - 独立 Flask 仍可继续使用；内嵌页与独立页共享同一份 `selfie_image_config.json` 运行配置。

## Web 管理页

- 基础设置：Web 状态、图片缓存上限、可使用人员、黑名单、白名单用户/群组
- 渠道管理：列表概要、弹窗编辑、新增/复制/删除生图渠道和审核渠道、刷新模型缓存、搜索缓存、启用模型顺序、模型优先级
- 渠道监控：表格显示时间/来源/状态/模型，详情查看请求数据、响应数据、请求图、生成图和来源身份
- 渠道测试：请求数据、响应数据、生成结果分区查看，可开关提示词增强，成功后自动切到结果
- 生图设置：默认比例/分辨率、并发、超时、图片大小、额度、冷却和 LLM 工具开关
- 形象设置：编辑自拍人设、上传/预览/清除自拍形象参考图
- 生图审核：提示词屏蔽词、审核白名单（默认跟随白名单用户/群组）、提示词审核、出图审核、OCR / 识图模型、审核模板；审核模型只从审核渠道选择
- JSON：插件独立配置兜底编辑，不包含 AstrBot 启动用的 Web host/port/token

基础设置、生图设置、形象设置、审核开关和模型顺序等常用项会自动保存并立即更新运行中的插件配置；保存按钮保留作兜底。

请求图和生成图统一保存在插件数据目录的 `plugin_data/astrbot_plugin_selfie_image/image_cache`，监控记录只保存路径，不保存 base64。出图审核拦截的生成图也会保留路径和文件，方便在后台查看。缓存超过基础设置里的上限后，会自动清理最旧缓存图直到低于上限。

## 已迁移范围

- 生图渠道：`openai`、`gemini`、`gemini_openai`、`z_image_gitee`、`jimeng2api`、`grok`、`agnes`
- 指令：`画`（别名 `生图`）、`文生图`、`图生图`、`自拍`（别名 `看看`）、`看看腿`、`看看你`、`合影`（别名 `合照`）、`形象查看`、`形象设置`、`形象清除`、`形象刷新`
- LLM 工具：`generate_image`、`generate_selfie`
- Web：基础设置、渠道管理、渠道监控、渠道测试、生图设置、形象设置、生图审核、JSON 编辑
