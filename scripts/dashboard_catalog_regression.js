#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const panels = new Map();
const persisted = new Map();
let apiCalls = 0;
function element(id) {
  if (!panels.has(id)) panels.set(id, {
    innerHTML: '', classList: {add() {}, remove() {}},
    querySelector() { return null; }, querySelectorAll() { return []; },
  });
  return panels.get(id);
}
const context = vm.createContext({
  console, setTimeout:(callback, delay) => setTimeout(callback, delay === 15000 ? 50 : delay), clearTimeout, queueMicrotask, URLSearchParams, AbortController,
  $: element, escapeHtml: String, window: {__TEST_PRESET_OPEN: true, __TEST_COS_OPEN: true},
  STUDIO: {nodePresetOpen: true, nodeCosOpen: true},
  PICKER_PAGE_SIZE: 30, PAGE_PREFETCH_AHEAD: 0,
  PICKER_CATALOG_CACHE_TTL_MS: 30000, PICKER_CATALOG_CACHE_VERSION: 0,
  PICKER_PAGE_CACHE: new Map(), PICKER_PAGE_REQUEST_GENERATIONS: new Map(),
  MANAGED_PAGE_CACHE: new Map(), MANAGED_PAGE_REQUEST_GENERATIONS: new Map(),
  PRESET_MANAGER_FILTERS: {}, PRESET_MANAGER_REVISIONS: {}, COS_POOL_REVISION: 0,
  PICKER_PANEL_STATES: new Map(), PICKER_LOADING: new Set(), PICKER_SEARCH_TIMERS: new Map(),
  ensureDashboardPersistentCachesHydrated: async () => {},
  readDashboardPersistCacheEntry: (namespace, key) => persisted.get(namespace+key),
  writeDashboardPersistCache: (namespace, key, data) => persisted.set(namespace+key, {data}),
  removeDashboardPersistCache: (namespace, key) => persisted.delete(namespace+key),
  api: async () => { apiCalls++; return {data:{items:[],total:0}}; },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../pages/dashboard/source/prompt_catalogs.js'), 'utf8'), context);

(async () => {
  // No CREATIVE_CANVAS global: the removed workspace must never block live pickers.
  for (const [panel, kind] of [['testPresetPanel', 'image'], ['testCosPanel', 'cos'], ['studioNodePresetPanel', 'image'], ['studioNodeCosPanel', 'cos']]) {
    await assert.doesNotReject(vm.runInContext(`loadCatalogPickerPage('${panel}', '${kind}')`, context));
    assert.equal(context.PICKER_LOADING.has(panel), false, `${panel} must leave the loading state`);
    assert.equal(context.PICKER_PANEL_STATES.get(panel).loaded, true);
  }
  for (const cache of [context.PICKER_PAGE_CACHE, context.MANAGED_PAGE_CACHE]) {
    const fetch = cache === context.PICKER_PAGE_CACHE ? 'fetchCatalogPage' : 'fetchManagedCatalogPage';
    await vm.runInContext(`Promise.all(Array.from({length:8}, () => ${fetch}('image', '', 1)))`, context);
    const entry = [...cache.values()][0];
    assert(entry.data, 'successful pages must populate the memory cache');
    assert.equal(entry.request, null, 'settled requests must leave the pending cache');
    const calls = apiCalls;
    await vm.runInContext(`${fetch}('image', '', 1)`, context);
    assert.equal(apiCalls, calls, 'reopening a page should reuse memory cache');
    cache.clear();
    await vm.runInContext(`${fetch}('image', '', 1)`, context);
    assert.equal(apiCalls, calls, 'persisted pages should be reused after reload');
  }
  context.api = async () => { throw new Error('fixture catalog failure'); };
  await vm.runInContext("loadCatalogPickerPage('testPresetPanel', 'image', 1, true)", context);
  assert.match(context.PICKER_PANEL_STATES.get('testPresetPanel').error, /fixture catalog failure/);
  assert.equal(context.PICKER_LOADING.has('testPresetPanel'), false);
  context.api = () => new Promise(() => {});
  await vm.runInContext("loadCatalogPickerPage('testCosPanel', 'cos', 1, true)", context);
  assert.match(context.PICKER_PANEL_STATES.get('testCosPanel').error, /超时/);
  assert.equal(context.PICKER_LOADING.has('testCosPanel'), false);
  context.api = async () => ({data:{items:[],total:0}});
  await vm.runInContext("loadCatalogPickerPage('testCosPanel', 'cos', 1, true)", context);
  assert.equal(context.PICKER_PANEL_STATES.get('testCosPanel').error, '');
  console.log('catalog regression: removed workspace, cache reuse, timeout and retry passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
