"""Release gates reject incomplete inputs before emitting installable archives."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.scripts import build_release, release_checks as checks


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def version_fixture(root: Path) -> None:
    write(root, "core/constants.py", 'PLUGIN_VERSION = "1.2.3"\n')
    write(root, "metadata.yaml", "name: astrbot_plugin_selfie_image\nversion: 1.2.3\n")
    write(root, "README.md", "- 当前稳定版：`1.2.3`\nEarlier 1.0.0\n")
    write(root, "CHANGELOG.md", "# Changes\n\n## 1.2.3 - 2026-11-14\n\n## 1.2.2 - 2026-11-13\n")


def test_versions_read_current_declarations_and_ignore_history(tmp_path):
    version_fixture(tmp_path)
    assert checks.validate_versions(tmp_path) == "1.2.3"


@pytest.mark.parametrize("name,content", [
    ("metadata.yaml", "version: 1.2.2\n"),
    ("metadata.yaml", "version: 1.2.3\nversion: 1.2.2\n"),
    ("README.md", "- 当前稳定版：`1.2.2`\nHistorical 1.2.3\n"),
    ("CHANGELOG.md", "## 1.2.2 - 2026-11-14\n\n## 1.2.3 - 2026-11-13\n"),
    ("core/constants.py", 'PLUGIN_VERSION = "1.2.3"\nPLUGIN_VERSION = "1.2.2"\n'),
])
def test_versions_reject_missing_mismatched_or_ambiguous_current_version(tmp_path, name, content):
    version_fixture(tmp_path)
    write(tmp_path, name, content)
    with pytest.raises(checks.ReleaseCheckError):
        checks.validate_versions(tmp_path)


@pytest.mark.parametrize("name,payload", [
    ("data/common/selfie_snapshots.json", {"version": 1, "items": []}),
    ("data/group/compositions.json", {"version": 1, "items": [{"id": "x", "title": "x"}]}),
    ("data/nai/reference.json", {"version": 1, "control_tags": ["safe"]}),
    ("data/studio/builtin_prompts.json", {"version": 1, "items": [{"id": "x", "title": "x", "prompt": "x", "templates": "all"}]}),
    ("data/legs/views.json", {"version": True, "views": []}),
    ("pages/dashboard/_page.json", {"title": {"zh-CN": "x"}, "description": {"zh-CN": "x", "en-US": "x"}}),
    ("_conf_schema.json", {"web": {"type": "object", "items": {"enable": {"description": "x", "type": "bool", "default": "true"}}}}),
])
def test_json_contracts_reject_valid_json_with_missing_or_wrong_fields(name, payload):
    with pytest.raises(checks.ReleaseCheckError):
        checks.validate_json_contract(name, payload)


@pytest.mark.parametrize("content", [
    '{"version":1,"version":1,"items":["ok"]}',
    '{"version":1,"items":[NaN]}',
    '{"version":1,"items":[Infinity]}',
])
def test_json_reader_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path, content):
    name = "data/common/selfie_snapshots.json"
    write(tmp_path, name, content)
    with pytest.raises(checks.ReleaseCheckError, match="invalid JSON contract"):
        checks.validate_json_files(tmp_path, tracked=[name])


def test_catalog_file_reference_must_be_tracked_and_count_must_match(tmp_path):
    index = "data/cos/looks/index.json"
    name = "data/cos/looks/demo.json"
    row = {"id": "demo", "title": "Demo", "prompt": "Demo prompt", "cos_type": "Demo"}
    write(tmp_path, index, json.dumps({"schema_version": 1, "files": [{"file": "demo.json", "cos_type": "Demo", "count": 1}], "order": ["demo"]}))
    write(tmp_path, name, json.dumps({"schema_version": 1, "cos_type": "Demo", "items": [row]}))
    with pytest.raises(checks.ReleaseCheckError, match="untracked catalog file"):
        checks.validate_json_files(tmp_path, tracked=[index])
    assert checks.validate_json_files(tmp_path, tracked=[index, name]) == 2
    payload = json.loads((tmp_path / index).read_text())
    payload["files"][0]["count"] = 2
    write(tmp_path, index, json.dumps(payload))
    with pytest.raises(checks.ReleaseCheckError, match="count/cos_type mismatch"):
        checks.validate_json_files(tmp_path, tracked=[index, name])


def test_release_selection_excludes_research_runtime_data_and_arbitrary_untracked_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "REQUIRED_FILES", frozenset({"main.py"}))
    files = {
        "main.py": "print('runtime')\n",
        "features/unreviewed.py": "API_KEY = 'untracked'\n",
        "pages/status-preview/app.js": "preview\n",
        "data/cos/research/capture.json": "{}\n",
        "image_cache/generated.png": "generated\n",
        "selfie_image_config.json": "{}\n",
        "pages/dashboard/index.source.html": "source\n",
        "pages/dashboard/source/runtime.js": "source\n",
        "tests/test_private.py": "test\n",
        "scripts/operator.py": "operator\n",
    }
    for name, content in files.items():
        write(tmp_path, name, content)
    tracked = set(files).difference({"features/unreviewed.py"})
    assert checks.release_files(tmp_path, tracked=tracked) == ["main.py"]


def test_release_requires_new_local_import_to_be_explicitly_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "REQUIRED_FILES", frozenset({"main.py"}))
    write(tmp_path, "main.py", "from .features.unreviewed import value\n")
    write(tmp_path, "features/unreviewed.py", "value = 1\n")
    with pytest.raises(checks.ReleaseCheckError, match="omitted from release manifest"):
        checks.release_files(tmp_path, tracked={"main.py"})
    assert checks.release_files(tmp_path, tracked={"main.py", "features/unreviewed.py"}) == ["features/unreviewed.py", "main.py"]


def test_release_explicitly_adopts_only_named_new_runtime_sources(tmp_path, monkeypatch):
    name = "generation/counted_workflow.py"
    monkeypatch.setattr(checks, "REQUIRED_FILES", frozenset({"main.py"}))
    write(tmp_path, "main.py", "from .generation.counted_workflow import value\n")
    write(tmp_path, name, "value = 1\n")
    assert checks.release_files(tmp_path, tracked={"main.py"}) == [name, "main.py"]


def test_release_rejects_missing_required_files_and_symlinked_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "REQUIRED_FILES", frozenset({"main.py"}))
    with pytest.raises(checks.ReleaseCheckError, match="required release files"):
        checks.release_files(tmp_path, tracked=[])
    write(tmp_path, "real.py", "value = 1\n")
    (tmp_path / "main.py").symlink_to(tmp_path / "real.py")
    with pytest.raises(checks.ReleaseCheckError, match="symlinks"):
        checks.release_files(tmp_path, tracked={"main.py"})


def test_artifact_is_deterministic_exact_and_preserves_both_dashboard_entrypoints(tmp_path):
    root = tmp_path / "source"
    names = ["main.py", "pages/dashboard/index.html", "webui/index_fallback.py"]
    for name in names:
        write(root, name, f"source for {name}\n")
    first = checks.build_artifact(root, tmp_path / "one.zip", names)
    second = checks.build_artifact(root, tmp_path / "two.zip", names)
    assert first == second
    with zipfile.ZipFile(tmp_path / "one.zip") as archive:
        assert archive.namelist() == [f"{checks.PLUGIN_NAME}/{name}" for name in names]
        assert archive.read(f"{checks.PLUGIN_NAME}/pages/dashboard/index.html") == (root / names[1]).read_bytes()
    with pytest.raises(checks.ReleaseCheckError, match="overwrite"):
        checks.build_artifact(root, tmp_path / "one.zip", names)


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_UNCOMPRESSED_BYTES", "MAX_ARCHIVE_BYTES"])
def test_artifact_enforces_each_size_budget(tmp_path, monkeypatch, limit):
    write(tmp_path, "main.py", "payload = 'some contents'\n")
    monkeypatch.setattr(checks, limit, 1)
    with pytest.raises(checks.ReleaseCheckError, match="exceed"):
        checks.build_artifact(tmp_path, tmp_path / "release.zip", ["main.py"])


def test_build_command_emits_no_artifact_when_any_release_gate_fails(tmp_path, monkeypatch):
    output = tmp_path / "release.zip"
    seen = []

    def fail(command, **kwargs):
        seen.append((command, kwargs))
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_release.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_release.build_verified_release(output, root=tmp_path)
    assert not output.exists()
    assert seen[0][0][1:] == ["scripts/verify_release.py", "--require-node"]
    assert seen[0][1]["check"] is True


def test_build_command_refuses_overwrite_before_running_gates(tmp_path, monkeypatch):
    output = tmp_path / "release.zip"
    output.write_bytes(b"user artifact")
    monkeypatch.setattr(build_release.subprocess, "run", lambda *args, **kwargs: pytest.fail("gates should not run"))
    with pytest.raises(checks.ReleaseCheckError, match="overwrite"):
        build_release.build_verified_release(output, root=tmp_path)
    assert output.read_bytes() == b"user artifact"


def test_build_command_preserves_checked_archive_after_successful_gates(tmp_path, monkeypatch):
    root = tmp_path / "source"
    version_fixture(root)
    write(root, "main.py", "value = 1\n")
    names = {"main.py", "core/constants.py", "metadata.yaml", "README.md", "CHANGELOG.md"}
    monkeypatch.setattr(checks, "REQUIRED_FILES", frozenset(names))
    monkeypatch.setattr(checks, "git_files", lambda _root: names)
    calls = []
    monkeypatch.setattr(build_release.subprocess, "run", lambda command, **kwargs: calls.append(command))
    output = tmp_path / "artifacts/release.zip"
    report = build_release.build_verified_release(output, root=root)
    assert calls[0][1:] == ["scripts/verify_release.py", "--require-node"]
    assert report["version"] == "1.2.3"
    assert report["archive_bytes"] == output.stat().st_size
    with zipfile.ZipFile(output) as archive:
        assert archive.read(f"{checks.PLUGIN_NAME}/main.py") == b"value = 1\n"
