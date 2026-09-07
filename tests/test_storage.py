from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin


class TestStorage:
    def _plugin(self, root: str) -> GenerationStoreMixin:
        plugin = GenerationStoreMixin()
        plugin.data_dir = root
        plugin.records_path = os.path.join(root, "generation_records.json")
        plugin.records_db_path = os.path.join(root, "generation_records.sqlite3")
        plugin.media_sources_dir = os.path.join(root, "media_sources")
        plugin.generated_dir = os.path.join(root, "image_cache")
        plugin.config = SimpleNamespace(image_cache_limit_mb=10)
        plugin._records_lock = threading.RLock()
        plugin._records = []
        plugin._record_seq = 0
        os.makedirs(plugin.generated_dir, exist_ok=True)
        return plugin

    def test_legacy_json_migrates_to_sqlite_and_sidecar(self) -> None:
        # Kept as a unittest-compatible method so this file works in minimal CI.
        with tempfile.TemporaryDirectory() as root:
            inline = "data:image/png;base64," + base64.b64encode(b"x" * 4096).decode("ascii")
            records_path = Path(root) / "generation_records.json"
            records_path.write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "id": "legacy-1",
                                "success": True,
                                "generated_image_sources": [{"type": "base64", "value": inline}],
                                "generated_image_paths": ["generated.png"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            plugin = self._plugin(root)
            loaded = plugin._load_records()
            assert loaded[0]["id"] == "legacy-1"
            assert (Path(root) / "generation_records.sqlite3").is_file()
            assert (Path(root) / "generation_records.json.bak").is_file()
            payload = (Path(root) / "generation_records.sqlite3").read_bytes()
            assert inline.encode("ascii") not in payload

            plugin._records = loaded
            detail = plugin.get_record_for_web("legacy-1")
            assert detail["generated_image_sources"][0]["value"] == inline
            assert list((Path(root) / "media_sources").glob("*.json"))

    def test_asset_metadata_round_trips_through_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._commit_generation_record(
                {
                    "id": "asset-1",
                    "success": True,
                    "generated_image_paths": [],
                    "prompt": "a test prompt",
                }
            )
            result = plugin.update_record_asset_metadata(
                "asset-1", {"favorite": True, "pinned": True, "tags": "one, two", "note": "keep"}
            )
            assert result == {
                "id": "asset-1",
                "favorite": True,
                "pinned": True,
                "tags": ["one", "two"],
                "note": "keep",
            }
            reloaded = self._plugin(root)
            reloaded._records = reloaded._load_records()
            assert reloaded.get_asset_records(favorite=True)[0]["tags"] == ["one", "two"]

    def test_favorite_asset_cache_path_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            favorite = Path(plugin.generated_dir) / "favorite.png"
            ordinary = Path(plugin.generated_dir) / "ordinary.png"
            favorite.write_bytes(b"f" * (6 * 1024 * 1024))
            ordinary.write_bytes(b"o" * (6 * 1024 * 1024))
            plugin.config.image_cache_limit_mb = 10
            plugin._commit_generation_record(
                {"id": "fav", "success": True, "favorite": True, "generated_image_paths": ["favorite.png"]}
            )
            preview = plugin.get_cache_cleanup_preview()
            assert all(item["path"] != "favorite.png" for item in preview["would_delete"])

    def test_asset_query_paginates_and_combines_filters(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._records = [
                {
                    "id": "asset-a",
                    "time": "2026-09-07 12:00:00",
                    "source": "group",
                    "source_label": "群 A",
                    "used_model": "model-a",
                    "media_type": "image",
                    "success": True,
                    "favorite": True,
                    "tags": ["portrait"],
                    "original_prompt": "blue portrait",
                    "generated_image_paths": ["a.png"],
                },
                {
                    "id": "asset-b",
                    "time": "2026-09-06 12:00:00",
                    "source": "private",
                    "source_label": "私聊",
                    "used_model": "model-b",
                    "media_type": "video",
                    "success": False,
                    "pinned": True,
                    "tags": ["motion"],
                    "original_prompt": "red motion",
                    "generated_video_paths": ["b.mp4"],
                },
                {
                    "id": "asset-c",
                    "time": "2026-09-05 12:00:00",
                    "source": "group",
                    "source_label": "群 B",
                    "used_model": "model-a",
                    "media_type": "image",
                    "success": True,
                    "tags": ["portrait"],
                    "original_prompt": "green portrait",
                    "generated_image_paths": ["c.png"],
                },
            ]
            plugin._persist_records()

            rows, meta = plugin.query_asset_records(
                source="group",
                model="model-a",
                media_type="image",
                success=True,
                tag="portrait",
                keyword="portrait",
                offset=1,
                limit=1,
            )
            assert [row["id"] for row in rows] == ["asset-c"]
            assert meta == {"total": 3, "filtered": 2, "offset": 1, "limit": 1}

    def test_batch_asset_metadata_and_delete_preserve_shared_cache(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            shared = Path(plugin.generated_dir) / "shared.png"
            unique = Path(plugin.generated_dir) / "unique.png"
            shared.write_bytes(b"shared")
            unique.write_bytes(b"unique")
            plugin._records = [
                {"id": "asset-a", "generated_image_paths": ["shared.png", "unique.png"], "success": True},
                {"id": "asset-b", "generated_image_paths": ["shared.png"], "success": True},
            ]
            plugin._persist_records()

            result = plugin.update_records_asset_metadata(
                ["asset-a", "asset-b"], {"favorite": True, "tags": ["batch"]}
            )
            assert result["updated_count"] == 2
            assert all(row["favorite"] for row in plugin.get_asset_records())
            deleted = plugin.delete_records(["asset-a"])
            assert deleted["deleted_count"] == 1
            assert shared.is_file()
            assert not unique.is_file()
            plugin.delete_records(["asset-b"])
            assert not shared.is_file()

    def test_asset_metadata_export_and_import_only_updates_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._records = [
                {
                    "id": "asset-a",
                    "success": True,
                    "favorite": True,
                    "pinned": False,
                    "tags": ["old"],
                    "note": "before",
                    "generated_image_sources": [{"type": "base64", "value": "data:image/png;base64,SECRET"}],
                    "generated_image_paths": ["a.png"],
                }
            ]
            plugin._persist_records()
            exported = plugin.export_asset_metadata()
            assert exported["format"] == "selfie-image-asset-metadata"
            assert exported["records"][0]["id"] == "asset-a"
            assert "SECRET" not in json.dumps(exported, ensure_ascii=False)

            imported = plugin.import_asset_metadata(
                {
                    "version": 1,
                    "records": [
                        {"id": "asset-a", "favorite": False, "pinned": True, "tags": ["new"], "note": "after"},
                        {"id": "missing", "favorite": True},
                    ],
                }
            )
            assert imported["updated"] == ["asset-a"]
            assert imported["missing"] == ["missing"]
            assert imported["updated_count"] == 1
            current = plugin.get_asset_records()[0]
            assert current["favorite"] is False
            assert current["pinned"] is True
            assert current["tags"] == ["new"]
            assert current["note"] == "after"

    def test_asset_tag_list_returns_counts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._records = [
                {"id": "a", "tags": ["portrait", "blue"]},
                {"id": "b", "tags": ["portrait"]},
            ]
            plugin._persist_records()
            assert plugin.list_asset_tags() == [
                {"tag": "portrait", "count": 2},
                {"tag": "blue", "count": 1},
            ]
