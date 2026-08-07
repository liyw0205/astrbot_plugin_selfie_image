# 前端视觉与静态帮助图方案

> 日期：2026-08-07  
> 参考：`astrbot_plugin_image_generation` Dashboard（圆角卡片 / eyebrow / 统计卡 / 明暗 token）  
> 约束：帮助图 **预生成入仓**，运行时 **禁止再调渠道生成**

---

## 1. 已完成

| 项 | 说明 |
|----|------|
| `logo.png` | 由当前形象参考图中心偏上裁 512×512 |
| `assets/help_poster.png` | 开发时用测试渠道+**原形象图**预生成；提示词强调 **禁止二次元化**、锁脸锁发型（见 `HELP_POSTER_PROMPT.md`） |
| `/生图帮助` | 优先发送仓库静态帮助图 + 文本指令列表；**无**「刷新图/运行时生成」 |

路径优先级：`assets/help_poster.png`（主）→ 根目录 `help_poster.png`（兼容）。

---

## 2. 前端为何丑 / 对照参考

| | Selfie 现状 | 通用生图参考 |
|--|-------------|--------------|
| 布局 | 8 平铺 tab + 表单感 | overview/generate/tasks/gallery + 大卡片 |
| Token | 硬编码灰底蓝钮 | `--primary #3c96ca`、大圆角、柔和阴影、可 dark |
| 品牌 | 无 logo 头图 | header + pill |
| 帮助 | 曾纯文本 | 现静态海报 |

### 优化分阶段（仍保持单文件 HTML，不引入 npm）

**P0 视觉 token**（建议下一 PR）  
对齐 img_gen：背景 `#f6f8fb`、圆角 12–16、阴影、header 浅色+logo、nav 胶囊。

**P1 信息架构**  
设置 / 渠道 / 测试 / 监控 / 形象 五类归并。

**P2**  
暗色、灯箱、空状态用 logo。

---

## 3. 帮助图再制作（开发机，不进运行时）

```bash
# 仅开发时：渠道 key 在本机 plugin_data，不提交
# 用 糖心/gpt-image-2 + logo.png 出图 → assets/help_poster.png
```

验收：仓库含 `logo.png` + `assets/help_poster.png`；`/生图帮助` 不出现「刷新图」；代码无 `_generate_help_poster`。

---

## 4. 验收清单

- [x] logo 来自当前形象  
- [x] 帮助图预生成入仓  
- [x] 命令只读静态图  
- [ ] 前端 P0 CSS（方案已写，可另开改动）
