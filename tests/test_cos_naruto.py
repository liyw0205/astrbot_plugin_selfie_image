"""Check reviewed Naruto looks through series selection and final prompts."""

import pytest

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_LOOK_SETS,
    build_cos_look_action,
    get_cos_look_by_id,
    list_cos_look_sets,
    match_cos_look_sets,
    normalize_cos_prompt_negatives,
    pick_cos_look_set,
)
from astrbot_plugin_selfie_image.features.persona import PersonaManager


CHARACTERS = {
    "sakura": ("春野樱", "小樱", "Sakura"),
    "hinata": ("日向雏田", "雏田", "Hinata"),
    "ino": ("山中井野", "井野", "Ino"),
    "tsunade": ("纲手", "Tsunade"),
    "temari": ("手鞠", "Temari"),
    "tenten": ("天天", "Tenten"),
    "mei": ("照美冥", "Mei Terumi"),
    "konan": ("小南", "Konan"),
    "kurenai": ("夕日红", "Kurenai"),
    "anko": ("御手洗红豆", "红豆", "Anko"),
    "samui": ("萨姆依", "萨姆伊", "Samui"),
}
NARUTO_IDS = {item["id"] for item in COS_LOOK_SETS if item["cos_type"] == "火影忍者"}


@pytest.mark.parametrize("series", ["火影忍者", "火影", "火影忍者疾风传", "Naruto", "Naruto Shippuden"])
def test_naruto_series_and_character_selection_keep_the_requested_scope(series):
    assert len(NARUTO_IDS) == 12
    assert {item["id"] for item in match_cos_look_sets(series)} == NARUTO_IDS
    for character, aliases in CHARACTERS.items():
        expected = {look_id for look_id in NARUTO_IDS if look_id.startswith(f"naruto_{character}_")}
        assert len(expected) == (2 if character == "sakura" else 1)
        for alias in aliases:
            assert {item["id"] for item in match_cos_look_sets(alias)} == expected
            assert {item["id"] for item in match_cos_look_sets(f"{series} {alias}")} == expected
    assert match_cos_look_sets(f"{series} 原神") == []
    assert pick_cos_look_set(query=f"{series} 西施") == {}


@pytest.mark.parametrize("camera", ["third", "first", "selfie"])
@pytest.mark.parametrize("look_id", sorted(NARUTO_IDS))
def test_naruto_outfit_is_preserved_once_in_final_prompt(tmp_path, look_id, camera):
    look = get_cos_look_by_id(look_id)
    action = build_cos_look_action("冷白洗手台", force_id=look_id, camera=camera)
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True,
        has_external_reference=False,
    )
    assert f"【cos:{look_id}】" in final
    assert "【cos_scene:cool_washbasin】" in final
    costume_contract = look["prompt"].split("。", 1)[0]
    assert final.count(costume_contract) == 1
    assert "以形象图中的成年女性为COS主体" in final
    assert "握持唯一一部普通手机" not in final


def test_naruto_looks_are_available_in_dashboard_catalog():
    looks = {item["id"]: item for item in list_cos_look_sets()}
    assert NARUTO_IDS <= looks.keys()
    for look_id in NARUTO_IDS:
        assert "Naruto" in looks[look_id]["series_aliases"]
    assert [item["id"] for item in match_cos_look_sets("火影 小樱 私设薄绿开衫抹胸")] == [
        "naruto_sakura_private_sage_bandeau"
    ]
