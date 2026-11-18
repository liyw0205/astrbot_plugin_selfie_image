#!/usr/bin/env node
'use strict';

// Static contract smoke for the dashboard identity pool. Browser coverage lives
// alongside the existing dashboard smoke tests; this keeps the check runnable
// without a browser when packaging a release.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'pages/dashboard/index.source.html'), 'utf8');
const auth = fs.readFileSync(path.join(root, 'pages/dashboard/source/selfie_auth.js'), 'utf8');
const bootstrap = fs.readFileSync(path.join(root, 'pages/dashboard/source/bootstrap.js'), 'utf8');
const styles = fs.readFileSync(path.join(root, 'pages/dashboard/source/styles.css'), 'utf8');

for (const id of [
  'selfiePersonaEnabled', 'selfieImagePoolPanel', 'selfieImagePoolList',
  'selfiePersonaPoolPanel', 'selfiePersonaPoolList', 'selfiePersonaName',
  'selfiePersonaPrompt', 'addSelfiePersona', 'refreshSelfieBtn', 'clearSelfieBtn',
]) assert.match(page, new RegExp(`id="${id}"`), `missing ${id}`);
for (const action of ['image_select', 'image_remove', 'persona_select', 'persona_remove', 'persona_add']) {
  assert.match(auth, new RegExp(`action:'${action}'`), `missing identity action ${action}`);
}
assert.match(auth, /api\('\/api\/selfie-identity'/, 'identity endpoint is not used');
assert.match(auth, /for \(let slot = 1; slot <= limit; slot \+= 1\)/, 'stable pool slots are not rendered');
assert.match(bootstrap, /selfiePersonaEnabled.*saveSelfieIdentityMode/s, 'mode switch is not wired');
assert.match(styles, /@media \(max-width: 520px\)[\s\S]*selfie-identity-row/, 'mobile pool layout is missing');
console.log('[identity-pool-smoke] selectors, actions, endpoint and mobile layout passed');
