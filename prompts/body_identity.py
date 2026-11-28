"""Body continuity rules shared by the final selfie prompt builders."""

from __future__ import annotations


def build_body_identity_prompt(
    *,
    language: str = "zh",
    has_identity_reference: bool = False,
    is_group: bool = False,
    is_legs: bool = False,
    is_retry: bool = False,
) -> str:
    if language == "en":
        lines = [
            "Body continuity: use the selected main identity reference only for reliably visible build, "
            "shoulder/chest/waist/hip relationships, limb thickness, and torso-to-limb proportions."
            if has_identity_reference else
            "Body continuity: use only body traits explicitly established in the main subject's current character settings; "
            "do not borrow a body from clothing references or companions.",
            "A headshot, partial view, loose clothing or occlusion leaves unseen or uncertain body traits unknown. "
            "Complete missing parts using explicit settings and natural anatomy; do not infer build from faces, names or clothing models, "
            "or invent measurements or a fixed body template.",
            "Fit outfits to the person; clothing references and costume character designs do not supply a replacement build. "
            "Allow natural perspective, foreshortening, compression and occlusion; keep the selected pose, camera and crop "
            "without copying the reference pose, stretching limbs or forcing a full body.",
            "Only an explicit user request may change named body traits or proportions, including chibi; keep other identity traits stable. "
            "Outfit, pose or style keywords alone do not request a new build.",
        ]
        if is_group:
            lines.append(
                "For a group redraw, match each person to the same identity across the supplied references; "
                "a previous group image may contain several people and is not a single person's body reference."
                if is_retry and has_identity_reference else
                "For the group, reference image 1 anchors only the main subject; companion references anchor their respective people."
                if has_identity_reference else
                "The main subject has no identity image; all supplied companion references belong to companions, never to the main subject."
            )
            lines.append(
                "Keep each person's visible build and proportions independently tied to their own reference or explicit settings; "
                "never average or swap bodies between people. Headshot-only and non-human references do not establish human body traits. "
                "A coherent group style does not require identical builds, heights, or proportions."
            )
            lines.append(
                "For companions requiring humanization or an explicitly requested gender conversion, apply companion conversion first; "
                "preserve only compatible human traits afterward, never lock source species anatomy, toy structure or exaggerated chibi proportions. "
                "Unknown human body traits do not prohibit creating a natural human companion."
            )
        if is_legs:
            lines.append(
                "Keep confirmed leg shape, thickness, and thigh-to-calf relationships; unknown legs remain unspecified. "
                "Use natural pose/view projection; never lengthen or narrow legs to fill the crop."
            )
    else:
        lines = [
            "身材保持：仅以本次选定的主角身份参考中可靠可见的体型、肩胸腰胯关系、四肢粗细和躯干四肢比例为依据。"
            if has_identity_reference else
            "身材保持：主角仅沿用当前人设中明确设定的身体特征，不借用服装参考或同伴的身材。",
            "身份参考只有头像、局部入镜、宽松衣物或遮挡时，未展示或无法确认的身体特征视为未知；"
            "只按当前人设的明确设定与自然人体结构补全本次取景所需部分，不从脸型、角色名或服装模特推断身材，不编造尺寸或套用固定身材模板。",
            "服装和配饰适配本人身体，不迁移服装参考人物或角色原设的体型。"
            "姿势、机位透视、近大远小、肢体受压和遮挡可自然改变可见轮廓；"
            "保持本次已选姿势、机位和裁切，不复制原图姿势、不拉伸肢体，也不为保身材扩大取景或强行补出全身。",
            "只有用户明确要求时才改变指定的身体特征或风格化比例，包括明确要求的 Q 版变换，其余已确认身份特征继续保持；"
            "套装、姿势和画风词本身不代表要求更换体型。",
        ]
        if is_group:
            lines.append(
                "合影重绘先按人物身份对应各参考中的同一人；上一张合影可能包含多人，不把整张多人图当作某一人的身体参考。"
                if is_retry and has_identity_reference else
                "合影中参考图一只锚定主角，同伴参考分别锚定各自人物。"
                if has_identity_reference else
                "主角没有身份参考图，所有输入的同伴参考只属于同伴，不得据此替换主角身材。"
            )
            lines.append(
                "每个人的可见体型和比例分别跟随其自身参考或明确人设，不在人物间平均、互换或混用；"
                "只有头像或非人物参考时不臆测其人体身材。统一画风不等于统一各人的体型、身高或头身比例。"
            )
            lines.append(
                "需要拟人或按用户要求转换性别的同伴，先执行合影对象转换规则，再保持转换后兼容的可确认人体特征；"
                "不锁定原物种肢体、玩具结构或夸张Q版比例，未知人体身材不代表禁止生成自然人类同伴。"
            )
        if is_legs:
            lines.append(
                "腿部取景仅保持身份参考或明确人设能确认的腿型、粗细与大腿小腿关系，未知腿部不预设；"
                "不为填满裁切画幅拉长或缩窄双腿，按选定机位和姿势呈现自然透视。"
            )
    return "\n".join(lines)
