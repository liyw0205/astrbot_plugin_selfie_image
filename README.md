# AstrBot AICat 生图自拍

这是从 `napcat-plugin-aicat` 迁移出的 AstrBot 插件，只保留生图、参考图图生图、AI 自拍、LLM 工具调用和 Flask Web 管理页。

## 使用步骤

1. 将 `astrbot_plugin_aicat` 放入 AstrBot 的插件目录并安装依赖：

   ```bash
   pip install -r astrbot_plugin_aicat/requirements.txt
   ```

2. 在 AstrBot 插件配置中只设置 Web 入口：

   - `web.enable`
   - `web.host`
   - `web.port`
   - `web.token`

   这些配置只在插件启动时读取。生图渠道、模型、自拍、人设、权限等完整配置请在 Flask Web 面板中保存，文件单独存放在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_aicat/aicat_config.json`。

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

4. 重载插件后使用命令：

   - `/画 一只白猫坐在窗边 --ar 1:1`
   - `/自拍 看着镜头自然自拍 --ar 3:4`
   - `/形象设置` 并附带图片或引用图片
   - `/形象查看`
   - `/形象清除`
   - `/aicat帮助`

5. LLM 可调用工具：

   - `generate_image(prompt, count, aspect_ratio, resolution)`
   - `generate_selfie(action, count, aspect_ratio, resolution)`

6. Flask Web 默认地址：

   - `http://127.0.0.1:14514`
   - 默认 Token：`changeme`
   - 修改 `web.port`、`web.host` 后需要重载插件让监听端口生效。
   - 打开后先进入登录页，输入 `web.token` 登录。

## Web 管理页

- 基础设置：Web 状态、图片缓存上限、可使用人员、黑名单、白名单用户/群组
- 渠道管理：列表概要、弹窗编辑、新增/复制/删除生图渠道和审核渠道、刷新模型缓存、搜索缓存、启用模型顺序、模型优先级
- 渠道监控：表格显示时间/来源/状态/模型，详情查看请求数据、响应数据、请求图、生成图和来源身份
- 渠道测试：请求数据、响应数据、生成结果分区查看，成功后自动切到结果
- 生图设置：默认比例/分辨率、并发、超时、图片大小、额度、冷却和 LLM 工具开关
- 形象设置：编辑自拍人设、上传/预览/清除自拍形象参考图
- 生图审核：提示词屏蔽词、审核白名单（默认跟随白名单用户/群组）、提示词审核、出图审核、OCR / 识图模型、审核模板；审核模型只从审核渠道选择
- JSON：插件独立配置兜底编辑，不包含 AstrBot 启动用的 Web host/port/token

基础设置、生图设置、形象设置、审核开关和模型顺序等常用项会自动保存并立即更新运行中的插件配置；保存按钮保留作兜底。

请求图和生成图统一保存在插件数据目录的 `plugin_data/astrbot_plugin_aicat/image_cache`，监控记录只保存路径，不保存 base64。缓存超过基础设置里的上限后，会自动清理最旧的 10 张旧缓存图。

## 已迁移范围

- 生图渠道：`openai`、`gemini`、`gemini_openai`、`z_image_gitee`、`jimeng2api`、`grok`
- 指令：`画`、`自拍`、`形象查看`、`形象设置`、`形象清除`、`形象刷新`
- LLM 工具：`generate_image`、`generate_selfie`
- Web：基础设置、渠道管理、渠道监控、渠道测试、生图设置、形象设置、生图审核、JSON 编辑

点歌、OenBot/红包、聊天管控、自定义指令、定时任务等没有迁入。
