# AstrBot Selfie Image 生图自拍

这是 AstrBot 生图自拍插件，包含生图、参考图图生图、AI 自拍、LLM 工具调用和 AstrBot WebUI 插件页控制台。

## 使用步骤

1. 将 `astrbot_plugin_selfie_image` 放入 AstrBot 的插件目录并安装依赖：

   ```bash
   pip install -r astrbot_plugin_selfie_image/requirements.txt
   ```

2. 在 AstrBot WebUI 的插件配置里保留占位配置即可。

   完整配置请进入 `插件管理 -> Selfie Image -> Dashboard` 页面管理。生图渠道、模型、自拍、人设、权限、审核和监控记录都会保存到 AstrBot 数据目录的 `plugin_data/astrbot_plugin_selfie_image/selfie_image_config.json` 等文件中。

3. 在 Dashboard 页面「渠道管理」里添加至少一个可用生图渠道；如需启用提示词审核、出图审核或 OCR / 识图，另行添加审核渠道。

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

## Dashboard 页面

- 基础设置：插件页接口状态、图片缓存上限、可使用人员、黑名单、白名单用户/群组
- 渠道管理：列表概要、弹窗编辑、新增/复制/删除生图渠道和审核渠道、刷新模型缓存、搜索缓存、启用模型顺序、模型优先级
- 渠道监控：表格显示时间/来源/状态/模型，详情查看请求数据、响应数据、请求图、生成图和来源身份
- 渠道测试：请求数据、响应数据、生成结果分区查看，可开关提示词增强，成功后自动切到结果
- 生图设置：默认比例/分辨率、并发、超时、图片大小、额度、冷却和 LLM 工具开关
- 形象设置：编辑自拍人设、上传/预览/清除自拍形象参考图
- 生图审核：提示词屏蔽词、提示词审核、出图审核、OCR / 识图模型、审核模板；审核模型只从审核渠道选择
- JSON：插件运行配置兜底编辑

基础设置、生图设置、形象设置、审核开关和模型顺序等常用项会自动保存并立即更新运行中的插件配置；保存按钮保留作兜底。

请求图和生成图统一保存在插件数据目录的 `plugin_data/astrbot_plugin_selfie_image/image_cache`，监控记录只保存路径，不保存 base64。出图审核拦截的生成图也会保留路径和文件，方便在后台查看。缓存超过基础设置里的上限后，会自动清理最旧缓存图直到低于上限。

## 已迁移范围

- 生图渠道：`openai`、`gemini`、`gemini_openai`、`z_image_gitee`、`jimeng2api`、`grok`、`agnes`
- 指令：`画`（别名 `生图`）、`文生图`、`图生图`、`自拍`（别名 `看看`）、`看看腿`、`看看你`、`合影`（别名 `合照`）、`形象查看`、`形象设置`、`形象清除`、`形象刷新`
- LLM 工具：`generate_image`、`generate_selfie`
- Dashboard：基础设置、渠道管理、渠道监控、渠道测试、生图设置、形象设置、生图审核、JSON 编辑
