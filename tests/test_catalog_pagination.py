import asyncio
import types

import astrbot_plugin_selfie_image.webui.dashboard_api as dashboard_api
from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI
from astrbot_plugin_selfie_image.webui.services import flatten_cos_pool_rows, paginate_catalog_rows


def test_catalog_page_filters_before_slicing_and_caps_payload_size():
    rows = [
        {"name": f"preset-{index}", "prompt": "portrait" if index % 2 else "landscape"}
        for index in range(75)
    ]

    page = paginate_catalog_rows(
        rows,
        offset=30,
        limit=30,
        query="portrait",
        fields=("name", "prompt"),
    )

    assert page["total"] == 37
    assert page["offset"] == 30
    assert len(page["items"]) == 7
    assert all("portrait" in row["prompt"] for row in page["items"])


def test_cos_catalog_flattening_deduplicates_builtin_ids_and_sorts_favorites_first():
    rows = flatten_cos_pool_rows(
        {
            "builtin": [
                {"id": "builtin", "title": "内置", "prompt": "built-in", "source": "builtin"},
                {"id": "fav", "title": "收藏", "prompt": "favorite", "source": "builtin", "favorite": True},
            ],
            "custom": [
                {"id": "builtin", "title": "重复自定义", "prompt": "duplicate", "source": "custom"},
                {"id": "custom", "title": "自定义", "prompt": "custom", "source": "custom"},
            ],
        }
    )

    assert [row["id"] for row in rows] == ["fav", "custom", "builtin"]
    assert next(row for row in rows if row["id"] == "builtin")["prompt"] == "built-in"


def test_dashboard_catalog_apis_return_only_requested_page():
    class Plugin:
        def list_prompt_presets_for_web(self, _kind):
            return [{"name": f"preset-{index}", "prompt": "match"} for index in range(65)]

        def list_managed_prompt_presets_for_web(self, _kind):
            return [{"name": f"preset-{index}", "prompt": "match"} for index in range(65)]

        def list_cos_pools_for_web(self):
            return {
                "builtin": [{"id": f"cos-{index}", "title": str(index), "prompt": "match", "source": "builtin"} for index in range(65)],
                "custom": [],
            }

    original_request, original_response = dashboard_api.request, dashboard_api.json_response
    dashboard_api.json_response = lambda payload, **_: payload
    api = SelfieImageDashboardAPI(Plugin())
    try:
        results = []
        for handler in (api.page_prompt_presets, api.page_prompt_presets_manage, api.page_cos_pools):
            dashboard_api.request = types.SimpleNamespace(query={"kind": "image", "limit": "30", "offset": "30", "search": "match"})
            results.append(asyncio.run(handler()))
    finally:
        dashboard_api.request, dashboard_api.json_response = original_request, original_response

    for response in results:
        payload = response["data"]
        page = payload["data"]
        assert payload["success"] is True
        assert page["offset"] == 30
        assert page["total"] == 65
        assert len(page["items"]) == 30
