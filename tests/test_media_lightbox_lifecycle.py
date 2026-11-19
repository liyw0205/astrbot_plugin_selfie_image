from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "pages/dashboard/source/record_details.js").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / "pages/dashboard/source/bootstrap.js").read_text(encoding="utf-8")
LIGHTBOX = SOURCE[
    SOURCE.index("    let MEDIA_LIGHTBOX_PATH = '';"):
    SOURCE.index("    function mediaSourceMime(")
]
DOWNLOAD = BOOTSTRAP[
    BOOTSTRAP.index("    if ($('mediaLightboxDownload')) {"):
    BOOTSTRAP.index("    $('presetAddBtn').onclick")
]


SCENARIOS = {
    "out_of_order_success_keeps_preview_and_download_on_latest_selection": r"""
open('A.jpg', 'A');
open('B.jpg', 'B');
requests[1].resolve({url: 'blob:B'});
await flush();
assert.equal(image.src, 'blob:B');
image.onload();
requests[0].resolve({url: 'blob:A'});
await flush();
assert.equal(image.src, 'blob:B');
assert.equal(image.alt, 'B');
elements.mediaLightboxDownload.onclick();
assert.deepEqual(downloads, [['B.jpg', 'B.jpg']]);
assert.deepEqual(requests.map(request => request.path), ['A.jpg', 'B.jpg']);
""",
    "late_error_does_not_replace_successful_preview": r"""
open('A.jpg');
open('B.jpg');
requests[1].resolve({url: 'blob:B'});
await flush();
image.onload();
requests[0].reject(new Error('A failed'));
await flush();
assert.equal(image.src, 'blob:B');
assert.equal(image.hidden, false);
assert.equal(status.hidden, true);
assert.notEqual(status.textContent, 'A failed');
""",
    "close_invalidates_pending_success_and_error": r"""
open('A.jpg');
close();
const before = JSON.stringify({src: image.src, hidden: image.hidden, status: status.textContent});
requests[0].resolve({url: 'blob:A'});
await flush();
assert.equal(JSON.stringify({src: image.src, hidden: image.hidden, status: status.textContent}), before);
assert.equal(elements.mediaLightboxModal.classList.contains('show'), false);
open('B.jpg');
close();
const closedStatus = status.textContent;
requests[1].reject(new Error('closed B failed'));
await flush();
assert.equal(status.textContent, closedStatus);
elements.mediaLightboxDownload.onclick();
assert.deepEqual(downloads, []);
""",
    "reopening_same_path_has_a_new_request_identity": r"""
open('same.jpg');
close();
open('same.jpg');
requests[0].resolve({url: 'blob:old'});
await flush();
assert.equal(image.src, '');
assert.equal(image.hidden, true);
requests[1].resolve({url: 'blob:new'});
await flush();
assert.equal(image.src, 'blob:new');
image.onload();
assert.equal(status.hidden, true);
""",
    "late_image_events_cannot_change_new_request_loading_or_success": r"""
open('A.jpg');
requests[0].resolve({url: 'blob:A'});
await flush();
const oldLoad = image.onload;
const oldError = image.onerror;
open('B.jpg');
oldLoad();
assert.equal(image.hidden, true);
assert.equal(status.hidden, false);
const loadingStatus = status.textContent;
oldError();
assert.equal(status.textContent, loadingStatus);
requests[1].resolve({url: 'blob:B'});
await flush();
image.onload();
oldError();
assert.equal(image.hidden, false);
assert.equal(status.hidden, true);
assert.equal(image.src, 'blob:B');
""",
    "late_image_events_after_close_do_not_reveal_image": r"""
open('A.jpg');
requests[0].resolve({url: 'blob:A'});
await flush();
const oldLoad = image.onload;
const oldError = image.onerror;
close();
const before = {status: status.textContent, hidden: status.hidden};
oldLoad();
oldError();
assert.equal(image.src, '');
assert.equal(image.hidden, true);
assert.equal(status.textContent, before.status);
assert.equal(status.hidden, before.hidden);
assert.equal(image.onload, null);
assert.equal(image.onerror, null);
""",
    "current_errors_and_reopen_still_work": r"""
open('broken.jpg');
requests[0].reject(new Error('read failed'));
await flush();
assert.equal(status.textContent, 'read failed');
assert.equal(status.hidden, false);
open('retry.jpg');
requests[1].resolve({url: 'blob:retry'});
await flush();
image.onerror();
assert.equal(image.hidden, true);
assert.equal(status.hidden, false);
assert.notEqual(status.textContent, 'read failed');
open('retry.jpg');
requests[2].resolve({url: 'blob:retry-success'});
await flush();
image.onload();
assert.equal(image.hidden, false);
assert.equal(status.hidden, true);
elements.mediaLightboxDownload.onclick();
assert.deepEqual(downloads, [['retry.jpg', 'retry.jpg']]);
""",
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_media_lightbox_lifecycle(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const classes = new Set();
const elements = {
  mediaLightboxModal: {classList: {
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
    contains(name) { return classes.has(name); },
  }},
  mediaLightboxImage: {
    src: '', hidden: true, onload: null, onerror: null,
    removeAttribute(name) { this[name] = ''; },
  },
  mediaLightboxStatus: {hidden: true, textContent: ''},
  mediaLightboxTitle: {textContent: ''},
  mediaLightboxDownload: {disabled: true},
};
const requests = [];
const downloads = [];
const context = vm.createContext({
  $: id => elements[id],
  getProtectedMedia(path) {
    return new Promise((resolve, reject) => requests.push({path, resolve, reject}));
  },
  protectedMediaUrl: media => media.url,
  downloadIconSvg: () => '',
  downloadCachePath: (path, filename) => downloads.push([path, filename]),
});
vm.runInContext(process.argv[2] + process.argv[3] + `
  globalThis.open = openMediaLightbox;
  globalThis.close = closeMediaLightbox;
`, context);
const {open, close} = context;
const image = elements.mediaLightboxImage;
const status = elements.mediaLightboxStatus;
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
""" + SCENARIOS[scenario] + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-", LIGHTBOX, DOWNLOAD],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
