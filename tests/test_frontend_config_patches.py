from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "pages" / "dashboard" / "source" / "configuration.js").read_text(encoding="utf-8")
RUNTIME = (ROOT / "pages" / "dashboard" / "source" / "runtime.js").read_text(encoding="utf-8")


def test_dashboard_tracks_server_config_baseline_for_domain_patches():
    assert "let CONFIG_SERVER_SNAPSHOT = null;" in RUNTIME
    assert "function rememberConfigServerSnapshot" in RUNTIME
    assert "function configPatchFromServerSnapshot" in SOURCE
    assert "const patch = options.patch || configPatchFromServerSnapshot" in SOURCE
    assert "patchDomains: ['proxies']" in SOURCE
    channel_source = (ROOT / "pages" / "dashboard" / "source" / "channel_editor.js").read_text(encoding="utf-8")
    assert "patchDomains:" in channel_source
    assert "EDITING_CHANNEL_KIND === 'audit' ? 'audit_channels' : 'image_channels'" in channel_source
    assert "patch: monitoringPatch" in (ROOT / "pages" / "dashboard" / "source" / "monitoring.js").read_text(encoding="utf-8")
    assert SOURCE.count("rememberConfigServerSnapshot(res.data)") >= 3
    assert "const requestPayload = {...payload};" in SOURCE
    assert "function restoreConfigFromServerSnapshot()" in SOURCE
    assert "restoreConfigFromServerSnapshot();" in SOURCE


def test_domain_patch_replaces_arrays_without_sending_unrelated_domains():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    start = SOURCE.index("    function configWriteBody(")
    end = SOURCE.index("    async function reloadConfigAfterConflict", start)
    helper = SOURCE[start:end]
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = process.argv[2];
const context = vm.createContext({JSON, Set, CONFIG_REVISION: 7, CONFIG_SERVER_SNAPSHOT: {
  image: {global_timeout: 280, blocked_words: ['old']},
  proxies: [{id: 'p1', host: 'old.example'}],
  image_channels: [{id: 'ch1', api_key: '******', enabled_models: ['old-model']}],
  permission: {usable_users: ['1']},
  unrelated: {keep: true},
}});
vm.runInContext(`
  ${source}
  globalThis.helpers = {patch: configPatchFromServerSnapshot};
`, context);
const current = {
  image: {global_timeout: 300, blocked_words: ['new']},
  proxies: [{id: 'p1', host: 'new.example'}],
  image_channels: [{id: 'ch1', api_key: '******', enabled_models: ['new-model']}],
  permission: {usable_users: ['1']},
  unrelated: {keep: true},
};
const patch = context.helpers.patch(current, ['image', 'proxies']);
assert.deepEqual(Object.keys(patch).sort(), ['image', 'proxies']);
assert.deepEqual(patch.image, current.image);
assert.deepEqual(patch.proxies, current.proxies);
assert.equal(patch.image_channels, undefined);
assert.equal(patch.permission, undefined);
assert.notStrictEqual(patch.proxies, current.proxies);
"""
    result = subprocess.run(
        [node, "-", helper], input=script, text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr

