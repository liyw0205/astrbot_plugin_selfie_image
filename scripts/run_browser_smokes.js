#!/usr/bin/env node
'use strict';

const path = require('node:path');
const {spawnSync} = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const SMOKE_SCRIPTS = [
  'mobile_smoke.js',
  'backup_smoke.js',
  'wardrobe_smoke.js',
  'dashboard_content_smoke.js',
  'identity_pool_browser_smoke.js',
];

function main(argv) {
  let chromium = '';
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--chromium') chromium = argv[++index] || '';
    else if (argument === '--list') {
      process.stdout.write(`${SMOKE_SCRIPTS.join('\n')}\n`);
      return 0;
    } else if (argument === '--help' || argument === '-h') {
      console.log('Usage: node scripts/run_browser_smokes.js [--chromium PATH] [--list]');
      console.log('Runs the dashboard browser smoke suites sequentially.');
      return 0;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }

  const env = chromium ? {...process.env, CHROMIUM_BIN: chromium} : process.env;
  for (const script of SMOKE_SCRIPTS) {
    console.log(`[browser-smoke] ${script}`);
    const result = spawnSync(process.execPath, [path.join(__dirname, script)], {
      cwd: ROOT,
      env,
      stdio: 'inherit',
    });
    if (result.error) throw result.error;
    if (result.status !== 0) return result.status || 1;
  }
  console.log('[browser-smoke] all suites passed');
  return 0;
}

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`[browser-smoke] ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {SMOKE_SCRIPTS, main};
