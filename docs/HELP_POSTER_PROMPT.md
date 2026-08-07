# Help poster generation notes (dev-time only; asset is shipped)

## Runtime
- `/生图帮助` → **image only** (`assets/help_poster.png` / `.jpg`)
- `/生图help` → **detailed text**
- **No live generation** of the poster

## Regenerate (developer machine)
Channel: prioritized test model (e.g. 糖心/gpt-image-2)  
Reference: current persona image (prefer raw persona webp)  
Aspect: vertical `2:3`

### Prompt principles
- Identity lock + eye contact into lens; no anime restyle
- Keep current card layout for commands
- **Do not** put English words like `QQ-bot` / `QQ bot` / brand names on the poster
- Footer line (Chinese only): `发 /生图help 查看指令详情`

### Prompt (English, used for gpt-image)
```
Create one clean vertical help poster for a Chinese chat bot image plugin.
Keep the same overall layout as a polished photo-poster (character + command cards).

IDENTITY (strict):
- Same person as the reference photo: face, hair, skin, likeness.
- Soft realistic photo-poster look, natural light. No anime / chibi / 2D cartoon.
- EYE CONTACT: look directly into the camera lens, friendly and focused.
  If the reference gaze is distracted, fix only the eyes to engage the viewer.

LAYOUT (keep structure, clean Chinese text only):
- Upper/mid: half-body or 3/4 guide character looking at camera.
- Mid/lower: neat Chinese command cards (high mobile readability):
  /画   /自拍   /合影
  /生图模型   /生图任务   /形象设置
- Bottom footer, single clear line in Chinese (must appear, legible):
  发 /生图help 查看指令详情
- Optional small title only in Chinese if needed: 生图帮助
- NO English labels. Especially do NOT write: QQ-bot, QQ bot, bot, Selfie Image, English slogans.
- No watermarks, no real brand logos, minimal decor, whitespace.

Use the reference only for identity fidelity.
```
