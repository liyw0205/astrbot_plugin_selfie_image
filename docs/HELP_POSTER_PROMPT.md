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
- **Eye contact**: if facing camera, must look into the lens; fix distracted/off-camera gaze without changing identity
- **Layout**: character guide + Chinese command cards (`/画 /自拍 /合影 /生图模型 /生图任务 /形象设置`)
- Clean whitespace, no watermarks/brands

### Prompt (English, used for gpt-image)
```
Create one clean vertical QQ-bot help poster.

IDENTITY (strict):
- Same person as the reference photo: face shape, eyes, nose, lips, skin, hair color/length/curl, overall likeness.
- Do NOT anime / chibi / 2D cartoon restyle. Soft realistic photo-poster look, natural light.
- EYE CONTACT: she is facing the camera and MUST look directly into the lens with a clear, friendly focus.
  If the reference looks distracted or off-camera, correct only the gaze so she engages the viewer; keep identity unchanged.
  Natural soft smile, present and attentive — not vacant, not looking away.

LAYOUT:
- Upper/mid: half-body or 3/4 of the character as a friendly guide, eye contact with camera.
- Lower/side: neat Chinese command cards, high readability on mobile:
  /画   /自拍   /合影
  /生图模型   /生图任务   /形象设置
- Optional small subtitle: 生图帮助
- Whitespace, minimal decor, no watermarks, no real brand logos.

Use the reference only for identity and appearance fidelity.
```
