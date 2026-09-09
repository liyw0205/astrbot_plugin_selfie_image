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
        plugin.config = SimpleNamespace(image_cache_limit_mb=10, image_cache_limit_count=10)
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

    def test_delivery_failure_is_visible_in_record_list_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._commit_generation_record(
                {
                    "id": "delivery-1",
                    "task_id": "cmd-12345678-1",
                    "success": True,
                    "generation_success": True,
                    "delivery_success": None,
                    "generated_image_paths": ["generated.png"],
                    "response_data": {"success": True},
                }
            )
            changed = plugin._update_generation_records_delivery(
                "cmd-12345678-1",
                delivered=False,
                error="发送失败",
                paths=["generated.png"],
            )
            assert changed == 1
            row = plugin.get_recent_records(summary=True)[0]
            assert row["status"] == "delivery_failed"
            assert row["error"] == "发送失败"
            detail = plugin.get_record_for_web("delivery-1")
            assert detail["delivery_success"] is False
            assert detail["response_data"]["delivery_error"] == "发送失败"

    def test_legacy_record_detail_backfills_channel_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._records = [
                {
                    "id": "legacy-route",
                    "success": True,
                    "used_model": "relay/image-model-v2",
                    "request_data": {"original_prompt": "old row"},
                    "attempts": [
                        {
                            "channel": "relay",
                            "model": "image-model-v2",
                            "success": True,
                        }
                    ],
                }
            ]
            detail = plugin.get_record_for_web("legacy-route")
            assert detail["channel"] == "relay"
            assert detail["model"] == "image-model-v2"
            assert detail["request_data"]["channel"] == "relay"
            assert detail["request_data"]["model"] == "image-model-v2"

    def test_task_links_are_backfilled_for_each_batch_record(self) -> None:
        """A completed batch exposes concrete record IDs, not only its task ID."""
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            plugin._web_tasks = {
                "web-12345678-1": {
                    "task_id": "web-12345678-1",
                    "status": "running",
                    "result": {},
                }
            }
            plugin._web_task_lock = threading.RLock()
            persisted = []
            plugin._persist_web_tasks_locked = lambda: persisted.append(True)

            plugin._commit_generation_records(
                {
                    "task_id": "web-12345678-1",
                    "success": True,
                    "count": 2,
                    "generated_image_paths": ["one.png", "two.png"],
                    "response_data": {"success": True, "count": 2},
                }
            )

            linked = plugin._web_tasks["web-12345678-1"]["record_ids"]
            assert len(linked) == 2
            assert set(linked) == {row["id"] for row in plugin._records}
            assert plugin._web_tasks["web-12345678-1"]["record_id"] == linked[0]
            assert plugin._web_tasks["web-12345678-1"]["result"]["record_ids"] == linked
            assert persisted

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

    def test_cache_cleanup_enforces_count_without_deleting_record_media(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            referenced = Path(plugin.generated_dir) / "referenced.png"
            referenced.write_bytes(b"r")
            plugin._commit_generation_record(
                {"id": "keep", "success": True, "generated_image_paths": ["referenced.png"]}
            )
            for index in range(11):
                path = Path(plugin.generated_dir) / f"orphan-{index:02}.png"
                path.write_bytes(bytes([index]))
                os.utime(path, (1000 + index, 1000 + index))

            preview = plugin.get_cache_cleanup_preview()
            assert preview["total_count"] == 12
            assert preview["would_delete_count"] == 2
            assert all(item["path"] != "referenced.png" for item in preview["would_delete"])

            cleaned = plugin._cleanup_image_cache_if_needed()
            assert len(cleaned["deleted"]) == 2
            assert cleaned["total_count"] == 10
            assert referenced.exists()

    def test_manual_cache_cleanup_requires_matching_preview_token(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plugin = self._plugin(root)
            for index in range(11):
                path = Path(plugin.generated_dir) / f"orphan-{index:02}.png"
                path.write_bytes(bytes([index]))
                os.utime(path, (1000 + index, 1000 + index))

            preview = plugin.cleanup_image_cache_from_web()
            assert preview["requires_confirmation"] is True
            plan = preview["preview"]
            assert plan["would_delete_count"] == 1
            assert len(plan["plan_token"]) == 32

            try:
                plugin.cleanup_image_cache_from_web(confirm=True, plan_token="stale-token")
            except ValueError as exc:
                assert "重新预览" in str(exc)
            else:
                raise AssertionError("stale cache cleanup token should be rejected")
            assert len(list(Path(plugin.generated_dir).iterdir())) == 11

            result = plugin.cleanup_image_cache_from_web(confirm=True, plan_token=plan["plan_token"])
            assert result["confirmed"] is True
            assert len(result["result"]["deleted"]) == 1
            assert result["result"]["total_count"] == 10

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
