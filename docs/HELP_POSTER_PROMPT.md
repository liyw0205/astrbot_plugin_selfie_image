# Help poster generation notes (dev-time only; asset is shipped)

## Runtime
`/生图帮助` only reads `assets/help_poster.png` (or `.jpg`). **No live generation.**

## Regenerate (developer machine)
Channel: prioritized test model (e.g. 糖心/gpt-image-2)  
Reference: current persona image (prefer raw persona webp over cropped logo)  
Aspect: vertical `2:3`

### Prompt principles
- **Identity lock**: same face/hair/skin as reference; no anime/chibi/2D restyle
- **Look**: soft realistic illustration / polished photo-poster, natural light
- **Layout**: character guide + Chinese command cards (`/画 /自拍 /合影 /生图模型 /生图任务 /形象设置`)
- Clean whitespace, no watermarks/brands

### Prompt (English, used for gpt-image)
```
Create a clean vertical QQ bot help poster (single image).

IDENTITY (strict):
- The mascot MUST be the exact same real person/character as the reference photo.
- Keep the same face shape, eyes, nose, lips, skin tone, hair color, hair length/curl pattern, and overall likeness.
- Do NOT turn her into anime, chibi, 2D cartoon, or cel-shaded art.
- Style: soft realistic illustration / polished photo-poster look, natural lighting, subtle depth of field. Tasteful, not plastic.

LAYOUT:
- Top or left: the character as a friendly guide, half-body or 3/4, calm smile, simple modern outfit consistent with reference vibe.
- Bottom or right: a neat instruction panel with clear Chinese labels on cards or a soft rounded board:
  /画   /自拍   /合影
  /生图模型   /生图任务   /形象设置
- Optional tiny subtitle: Selfie Image 生图帮助
- Plenty of whitespace, minimal decorations, no watermarks, no real brand logos, no cluttered UI chrome.
- High readability for mobile chat; balanced composition; finished poster quality.

Use the reference image only for character identity and appearance fidelity.
```
