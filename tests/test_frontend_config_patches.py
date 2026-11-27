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
    assert "kind === 'audit' ? 'audit_channels' : 'image_channels'" in channel_source
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


def test_model_cache_refresh_error_details_expand_upward_without_locking_channel_modal():
    root = Path(__file__).resolve().parents[1]
    channel_source = (root / "pages/dashboard/source/channel_editor.js").read_text(encoding="utf-8")
    bootstrap = (root / "pages/dashboard/source/bootstrap.js").read_text(encoding="utf-8")
    styles = (root / "pages/dashboard/source/styles.css").read_text(encoding="utf-8")
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")

    assert "function showModelRefreshError(error)" in channel_source
    assert "disclosure.append(summary, pre);" in channel_source
    assert "catch (e) { showModelRefreshError(e); }" in channel_source
    assert 'id="channelModalCloseButton"' in template
    assert "channelModalCloseButton')?.addEventListener('click', closeChannelModal)" in bootstrap
    assert "#channelModal .modal-footer { flex: 0 0 auto; overflow: visible; }" in styles
    assert "#channelModal #modalStatus details[open] pre" in styles
    assert "bottom: calc(100% + 8px)" in styles
    assert "max-height: min(38vh, 360px)" in styles


def test_priority_selectors_remain_filtered_after_model_selector_refresh():
    root = Path(__file__).resolve().parents[1]
    channel_source = (root / "pages/dashboard/source/channel_editor.js").read_text(encoding="utf-8")

    assert "prunePriorityList('image', labels);" in channel_source
    assert "prunePriorityList('audit', audits);" in channel_source
    assert "preserveSelection('priorityPicker'" not in channel_source
    assert "preserveSelection('auditPriorityPicker'" not in channel_source


def test_novelai_parameters_only_show_for_novelai_image_channels():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    channel_source = (root / "pages/dashboard/source/channel_editor.js").read_text(encoding="utf-8")

    assert 'id="modalNaiSettings" style="margin-top:12px;display:none"' in template
    assert "EDITING_CHANNEL_KIND === 'image' && ch.provider_type === 'novelai' ? '' : 'none'" in channel_source


def test_group_fixed_model_is_conditional_and_preview_mode_is_tristate():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    channel_source = (root / "pages/dashboard/source/channel_editor.js").read_text(encoding="utf-8")

    assert '<span>预览</span></div>' in template
    assert "const QQ_GROUP_PREVIEW_MODES = [['default', '默认'" in channel_source
    assert "['on', '开启'" in channel_source
    assert "['off', '关闭'" in channel_source
    assert "const fixedPanel = mode === 'fixed' ?" in SOURCE
    assert "openGroupConfigModal(${index}, 'fixed_model')" in SOURCE
    assert "openGroupConfigModal(${index}, 'preview_hidden_mode')" in SOURCE


def test_selfie_management_is_split_into_appearance_schedule_and_wardrobe_tabs():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    selfie_source = (root / "pages/dashboard/source/selfie_auth.js").read_text(encoding="utf-8")
    bootstrap = (root / "pages/dashboard/source/bootstrap.js").read_text(encoding="utf-8")

    for view in ("appearance", "schedule", "wardrobe"):
        assert f'data-selfie-view="{view}"' in template
        assert f'id="selfieView{view.title()}"' in template
    assert 'id="selfieViewSchedule" class="selfie-view-panel" role="tabpanel"' in template
    assert 'id="selfieViewWardrobe" class="selfie-view-panel" role="tabpanel"' in template
    assert "function switchSelfieView(view)" in selfie_source
    assert "tab.setAttribute('aria-selected', String(active));" in selfie_source
    assert "event.key === 'ArrowRight'" in bootstrap


def test_auxiliary_settings_are_tabbed_and_save_reverse_prompt_model():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    channel_source = (root / "pages/dashboard/source/channel_editor.js").read_text(encoding="utf-8")
    runtime = (root / "pages/dashboard/source/runtime.js").read_text(encoding="utf-8")
    bootstrap = (root / "pages/dashboard/source/bootstrap.js").read_text(encoding="utf-8")
    switcher = (root / "pages/dashboard/source/selfie_auth.js").read_text(encoding="utf-8")

    for view in ("review", "recognition", "translation"):
        assert f'data-audit-view="{view}"' in template
    assert 'id="reversePromptModel"' in template
    assert "CONFIG.image.reverse_prompt_model = $('reversePromptModel').value || '';" in runtime
    assert "img.reverse_prompt_model ??= '';" in runtime
    assert "preserveSelection('reversePromptModel', auditLabels, CONFIG.image?.reverse_prompt_model || '');" in channel_source
    assert "function switchAuditView(view)" in switcher
    assert "[data-audit-view]" in bootstrap


def test_prompt_preset_and_nai_transfer_use_json_files_like_cos_pool():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    catalog_source = (root / "pages/dashboard/source/prompt_catalogs.js").read_text(encoding="utf-8")
    bootstrap = (root / "pages/dashboard/source/bootstrap.js").read_text(encoding="utf-8")

    assert "导入文件" in template
    assert "导出文件" in template
    assert "function promptPresetFilePayload" in catalog_source
    assert "function downloadJsonFile" in catalog_source
    assert "application/json,.json" in catalog_source
    assert "importPromptPresetFilePayload(parsed, kind)" in catalog_source
    assert "parsed.kind !== kind" in catalog_source
    assert "nai-artist-presets" in catalog_source
    assert "image-presets" in catalog_source
    assert "/api/prompt-presets/manage/import" in catalog_source
    assert "presetTransferModal" not in template + bootstrap + catalog_source


def test_filter_and_generation_forms_pair_selects_and_keep_inputs_full_width():
    root = Path(__file__).resolve().parents[1]
    template = (root / "pages/dashboard/index.source.html").read_text(encoding="utf-8")
    styles = (root / "pages/dashboard/source/styles.css").read_text(encoding="utf-8")

    assert template.count('class="dashboard-filter-grid"') == 5
    assert ".dashboard-filter-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert ".dashboard-filter-grid > .filter-full { grid-column: 1 / -1; }" in styles
    assert 'id="metricAlertSettingsModal" class="modal-mask"' in template
    assert 'id="openMetricAlertSettings"' in template
    assert 'id="saveMetricAlertSettings"' in template
