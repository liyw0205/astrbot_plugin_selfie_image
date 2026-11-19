"""Exercise real dashboard loaders with delayed and reordered responses."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pages/dashboard/source/selfie_auth.js"

SCENARIOS = {
    "identity_ready_before_schedule_and_short_cache_reuses_requests": r"""
const first = refreshSelfie();
assert.equal(requests.length, 2);
assert.equal(el('selfiePersonaEnabled').disabled, true);
assert.equal(el('selfiePersonaEnabled').indeterminate, true);
respond('/api/selfie-identity?view=summary', identity('persona'));
await flush();
assert.equal(el('selfiePersonaEnabled').checked, true);
assert.equal(el('selfiePersonaEnabled').disabled, false);
assert.equal(el('selfiePersonaPoolPanel').hidden, false);
assert.equal(requests.length, 2, 'persona mode must not fetch images');
respond('/api/selfie-profile/ensure-today', daily('today'));
await first;
assert.ok(el('selfieDailySchedule').innerHTML.includes('today'));
await refreshSelfie();
assert.equal(requests.length, 2, 'reopening a fresh page must reuse small state');
now += 10000;
await refreshSelfie();
now += 6000;
const second = refreshSelfie();
const duplicate = refreshSelfie();
assert.equal(requests.length, 4, 'expired state should refresh once per section');
respond('/api/selfie-identity?view=summary', identity('persona'));
respond('/api/selfie-profile/ensure-today', daily('new today'));
await Promise.all([second, duplicate]);
""",
    "schedule_ready_while_identity_slow_or_failing": r"""
const task = refreshSelfie();
respond('/api/selfie-profile/ensure-today', daily('automatic today'));
await flush();
assert.ok(el('selfieDailySchedule').innerHTML.includes('automatic today'));
assert.equal(el('selfiePersonaEnabled').disabled, true);
take('/api/selfie-identity?view=summary').reject(new Error('identity unavailable'));
await task;
assert.ok(el('selfieDailySchedule').innerHTML.includes('automatic today'));
assert.equal(el('selfiePersonaEnabled').disabled, true);
""",
    "late_read_cannot_revert_saved_mode_or_cancel_schedule": r"""
renderSelfieIdentity(identity('image'));
now += 16000;
const oldRead = refreshSelfie();
const oldIdentity = take('/api/selfie-identity?view=summary');
el('selfiePersonaEnabled').checked = true;
const save = saveSelfieIdentityMode();
const mutation = take('/api/selfie-identity');
assert.equal(JSON.parse(mutation.options.body).view, 'summary');
mutation.resolve({data: identity('persona')});
await save;
oldIdentity.resolve({data: identity('image')});
respond('/api/selfie-profile/ensure-today', daily('still loads today'));
await oldRead;
assert.equal(el('selfiePersonaEnabled').checked, true);
assert.equal(el('selfiePersonaPoolPanel').hidden, false);
assert.ok(el('selfieDailySchedule').innerHTML.includes('still loads today'));
""",
    "new_refresh_and_wardrobe_mutation_reject_older_responses": r"""
const first = refreshSelfieIdentity();
const oldIdentity = take('/api/selfie-identity?view=summary');
const second = refreshSelfieIdentity(true);
respond('/api/selfie-identity?view=summary', identity('persona'));
await second;
oldIdentity.resolve({data: identity('image')});
await first;
assert.equal(el('selfiePersonaEnabled').checked, true);
const oldDaily = refreshSelfieDailySchedule();
const oldRequest = take('/api/selfie-profile/ensure-today');
beginSelfieMutation();
const newDaily = refreshSelfieDailySchedule();
respond('/api/selfie-profile/ensure-today', daily('new wardrobe'));
await newDaily;
oldRequest.resolve({data: daily('old wardrobe')});
await oldDaily;
assert.ok(el('selfieDailySchedule').innerHTML.includes('new wardrobe'));
assert.ok(!el('selfieDailySchedule').innerHTML.includes('old wardrobe'));
""",
    "failed_schedule_can_retry_without_resetting_identity": r"""
const task = refreshSelfie();
respond('/api/selfie-identity?view=summary', identity('persona'));
take('/api/selfie-profile/ensure-today').reject(new Error('schedule unavailable'));
await task;
assert.ok(el('selfieDailySchedule').innerHTML.includes('schedule unavailable'));
const retry = retrySelfieDailySchedule();
respond('/api/selfie-profile/ensure-today', daily('recovered today'));
await retry;
assert.ok(el('selfieDailySchedule').innerHTML.includes('recovered today'));
assert.equal(el('selfiePersonaEnabled').checked, true);
assert.equal(requests.filter(r => r.path.includes('identity')).length, 1);
""",
    "login_does_not_restore_unrelated_page_caches": r"""
const login = enterApp();
assert.equal(requests[0]?.path, '/api/config');
assert.equal(hydrationCalls, 0, 'login must not wait for record/asset cache hydration');
respond('/api/config', {});
await login;
assert.equal(hydrationCalls, 0);
assert.equal(requests.length, 1, 'login must not preload unopened data pages');
""",
    "external_selection_invalidates_previous_preview": r"""
renderSelfieIdentity({...identity('image'), has_image:true, active_image_slot:1,
  updated_at:'first', image:'data:image/png;base64,original'});
assert.equal(context.SELFIE_REFERENCE_DATA_URL, 'data:image/png;base64,original');
renderSelfieIdentity({...identity('image'), has_image:true, active_image_slot:2, updated_at:'second'});
assert.equal(context.SELFIE_REFERENCE_DATA_URL, '');
assert.equal(el('selfiePreview').style.display, 'none');
""",
    "startup_readiness_refreshes_open_selfie_page": r"""
DASHBOARD_VISITED_TABS.add('selfie');
const ready = refreshDashboardAfterStartupReady();
await new Promise(resolve => setTimeout(resolve, 20));
assert.ok(requests.some(r => r.path === '/api/selfie-identity?view=summary'));
assert.ok(requests.some(r => r.path === '/api/selfie-profile/ensure-today'));
respond('/api/selfie-identity?view=summary', identity('persona'));
respond('/api/selfie-profile/ensure-today', daily('startup today'));
assert.equal(await ready, true);
assert.ok(el('selfieDailySchedule').innerHTML.includes('startup today'));
""",
    "scope_change_invalidates_memory_and_inflight_reads": r"""
const old = refreshSelfie();
const oldIdentity = take('/api/selfie-identity?view=summary');
const oldDaily = take('/api/selfie-profile/ensure-today');
scope = 'other-account';
const current = refreshSelfie();
respond('/api/selfie-identity?view=summary', identity('persona'));
respond('/api/selfie-profile/ensure-today', daily('current account'));
await current;
oldIdentity.resolve({data: identity('image')});
oldDaily.resolve({data: daily('previous account')});
await old;
assert.equal(el('selfiePersonaEnabled').checked, true);
assert.ok(el('selfieDailySchedule').innerHTML.includes('current account'));
""",
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_selfie_load_order_and_state(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {
    checked:false, disabled:false, hidden:true, value:'', textContent:'', innerHTML:'', style:{},
    setAttribute() {}, removeAttribute() {}, querySelectorAll:() => [], querySelector:() => null,
  });
  return elements.get(id);
}
const requests = [];
let now = 1000, scope = 'initial-account', hydrationCalls = 0;
const context = vm.createContext({
  $, window:{}, document:{body:{classList:{add(){},remove(){}}},querySelector:() => null},
  get now() { return now; }, Date:{now:() => now},
  console, setTimeout:callback => setTimeout(callback, 0),
  protectedMediaScope:() => scope, ensureProtectedMediaScope:() => {},
  SELFIE_REFERENCE_VERSION:0, SELFIE_REFERENCE_DATA_URL:'', SELFIE_REFERENCE_REQUEST:null,
  AUTH_TOKEN:'', CONFIG:{}, CONFIG_REVISION:0, DASHBOARD_VISITED_TABS:new Set(),
  showToast:() => {}, loadSelfieReferenceDataUrl:async () => '', isDashboardPage:() => false,
  ensureDashboardPersistentCachesHydrated:() => { hydrationCalls++; return new Promise(() => {}); },
  rememberConfigServerSnapshot(){}, markConfigLoaded(){}, fillForms(){},
  safeStorageSet(){}, safeStorageRemove(){}, checkHealth:async () => ({}), resumeImageTestTask:async () => {},
  api:(path, options = {}) => {
    if (path === '/api/health') return Promise.resolve({data:{startup:{status:'ready'}}});
    return new Promise((resolve, reject) => requests.push({path, options, resolve, reject, taken:false}));
  },
});
function $(id) { return el(id); }
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const identity = mode => ({identity_mode:mode, image_pool:[], persona_pool:[], has_image:false});
const daily = title => ({wardrobe:{items:[]}, daily_schedule:{schedule:{
  date:'2026-11-20', events:[{id:'event',title}],
}}});
function take(path) {
  const request = requests.find(r => !r.taken && r.path === path);
  assert.ok(request, `Missing ${path}`); request.taken = true; return request;
}
function respond(path, data) { take(path).resolve({data}); }
const flush = () => new Promise(resolve => setImmediate(resolve));
for (const name of ['refreshSelfie', 'refreshSelfieIdentity', 'renderSelfieIdentity',
  'refreshSelfieDailySchedule', 'retrySelfieDailySchedule', 'saveSelfieIdentityMode',
  'beginSelfieMutation', 'enterApp', 'refreshDashboardAfterStartupReady']) {
  globalThis[name] = vm.runInContext(name, context);
}
globalThis.DASHBOARD_VISITED_TABS = context.DASHBOARD_VISITED_TABS;
(async () => { SCENARIO })().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCENARIO", SCENARIOS[scenario])
    result = subprocess.run(
        [node, "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
