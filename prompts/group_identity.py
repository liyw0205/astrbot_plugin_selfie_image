"""Companion conversion rules shared by group-photo prompt builders."""


def build_group_companion_identity_prompt(language: str = "zh") -> str:
    if language == "en":
        return (
            "Companion conversion: apply these rules only to designated group-photo companions, never to the main subject. "
            "References explicitly assigned as scenery, clothing or props keep that role.\n"
            "Real human companions retain their own identity, gender and confirmed natural build.\n"
            "Non-human subjects, animals, anime or illustrated characters, cartoons, chibi, memes, mascots, dolls, plush toys, "
            "landscapes, buildings, rooms, objects and color blocks designated as companions must become independent adult women. "
            "Only an explicit gender request in the user's current instruction overrides this female default; "
            "source-character gender, species, names or masculine accessories do not override it.\n"
            "Transfer recognizable colors, patterns, hair, accessories and personality into a human design in the main subject's medium. "
            "Do not retain the source species anatomy, toy materials, exaggerated chibi proportions or object structure. "
            "Convert each designated subject once; do not turn incidental background objects into extra people.\n"
            "Use natural adult human anatomy for converted companions without fixed measurements or a preset build. "
            "Body continuity and composition identity rules apply after conversion and must not cancel humanization or its gender rule.\n"
            "Each companion is an independent, anatomically complete person, separate from the main subject's body and clothes. "
            "Keep the selected crop and natural occlusion; complete anatomy does not require a full-body shot."
        )
    return (
        "合影对象转换：以下规则只作用于本次指定的同框对象，不改变 AI 主角身份；用户明确指定为背景、服装或道具的参考按该用途使用。\n"
        "真人同伴保留各自身份、性别与可确认的自然体型，不因拟人默认规则改变真人性别。\n"
        "非人、动物、二次元、动漫、插画、卡通、Q版、表情包、吉祥物、玩偶、毛绒玩具、景物、风景、建筑、房间、道具或纯色块作为同框对象时，"
        "自动拟人为独立的成年女性，默认成年女性；仅用户本次文字明确指定其他性别时覆盖，原型或角色原设性别、物种、名字与男性化配饰都不能覆盖此默认。\n"
        "只迁移可辨识的配色、纹样、发型、饰品与气质，设计为与主角同一媒介的完整人类角色；不保留原物种肢体、玩具材质、夸张Q版头身或景物结构。"
        "每个指定对象只拟人一次，不把背景中每个物件都增加为人物。\n"
        "拟人后的同伴采用自然成年人体结构，未知身材不写死尺寸或固定模板；身材保持及组合中的身份沿用作用于转换后，不能阻止本条拟人与性别转换。\n"
        "同框必须是独立完整人物，与主角身体和衣物分离，边界清晰，禁止粘连融合或原样保留玩偶外形；完整指人体结构连贯，仍服从本次机位、裁切和自然遮挡，不强迫拍全身。"
    )
