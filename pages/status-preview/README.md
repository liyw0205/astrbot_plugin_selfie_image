# 生图记录状态图片预览

五种 Pillow 排版，03「双栏卡片」已选为 `/生图记录状态` 的正式图片样式。预览只使用明确标注的演示数据，不读取实际配置或生图记录。

```sh
python pages/status-preview/server.py --port 8765
```

打开 `http://127.0.0.1:8765`，通过方案标签比较，或使用 `?v=1` 至 `?v=5` 直达对应方案。端口占用时，服务依次尝试后续 19 个端口，以启动输出为准。服务仅绑定本机回环地址。

提供混合状态、全部正常、暂无记录、长名称和多页预览，支持原尺寸查看、翻页及 PNG 下载。所有方案使用同一份状态结构，宽度统一为 1080 像素。

`renderer.render_status_report(status, design="1", page=1, page_size=6)` 接收当前模型状态数据并返回 Pillow Image，选定后可接入现有命令。中文字体自动寻找 Android、Linux 或 Windows 的常见字体，也可通过 `ASTRBOT_STATUS_FONT` 指定。
