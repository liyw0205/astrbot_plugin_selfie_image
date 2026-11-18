# Release Checks

Run every release gate before publishing:

```sh
python scripts/verify_release.py --require-node
```

`--layer python`, `--layer frontend`, `--layer static`, and `--layer package`
remain available for focused checks. The Python layer accepts
`--test-scope full|compatibility`; the latter is intended for non-canonical
interpreters in the CI matrix, and `--skip-static` omits compile/no-op checks
already covered by a separate release job. The static layer includes version
consistency, COS/leg prompt-pool compatibility and deep combination lint, bundled JSON contracts, a temporary runtime ZIP, compilation,
Dashboard checks, and `git diff --check HEAD`. The package layer validates the
version, JSON and runtime manifest without creating a throwaway ZIP; the
builder performs the one final archive round-trip immediately afterwards. Both
static and package layers do not run pytest. Local frontend checks may skip
Node when it is unavailable; release builds require Node explicitly.

To create an installable artifact locally, including the full pytest gate:

```sh
python scripts/build_release.py --output dist/astrbot_plugin_selfie_image.zip
```

The command runs the full gate by default and never overwrites an existing
output file. CI runs the Python and frontend gates first, then calls the same
builder with `--skip-tests`; this selects package-only validation so pytest,
compile/no-op, Dashboard checks and a throwaway ZIP are not repeated. The
builder still validates the manifest and performs the final archive round-trip.
The skip is intended for that already-gated CI composition, not as a local
replacement for the full command. Creating or editing the workflow locally
does not execute it.

Tag-triggered CI starts with a `release-tag` job. It requires the ref to match
`vMAJOR.MINOR.PATCH` and to equal the current version declarations before the
Python, Dashboard, or package jobs can start. Ordinary commits, branch pushes,
and pull requests do not trigger this workflow.

## Host Rehearsal

`scripts/host_rehearsal.py` is an operator-side rehearsal runner for a real,
isolated AstrBot instance. It refuses incomplete contracts before starting a
process. The JSON contract must provide a new or empty `data_root`, an existing
`working_directory`, foreground `start_command`/`stop_command` argv arrays, an
unauthenticated `health_url`, AstrBot/plugin/target/rollback versions, a test
channel and account, an explicit proxy mode (`none`, `http`, or `socks`), and
commands for all six phases: `upgrade`, `generate`, `failure`, `cancel`,
`restart`, and `rollback`. `start_command` must contain the bare `{data_root}`
placeholder in an argv item; otherwise preflight rejects the contract.

Commands are argv arrays, never shell strings. They may use `{data_root}`,
`{report_path}`, `{phase}`, `{target_version}`, `{rollback_version}`, and
`{channel}`. Proxy credentials are supplied through the configured environment
variable (`proxy.url_env`), never written to the contract or report.
The startup wrapper must apply `{data_root}` as AstrBot's effective data
directory. The runner verifies that the path is passed, but cannot inspect
host-specific configuration to prove AstrBot honored it; verify the effective
runtime path before treating the rehearsal as isolated.

Run it only with a disposable data root:

```sh
# Safe schema/path/command preflight; does not create directories or start the host.
python scripts/host_rehearsal.py --config /path/to/host-rehearsal.json --validate-only

# Full rehearsal; starts the isolated host and runs all six phases.
python scripts/host_rehearsal.py --config /path/to/host-rehearsal.json
```

Validation does not create directories. Immediately before starting the host,
the runner rechecks that `data_root` is still the same empty directory (or is
still absent) observed during preflight. It removes a newly created root only
if the path still identifies that same directory after the run; changed paths
are preserved and reported as failure. A pre-existing empty directory is
preserved, and `--keep-data` retains a newly created root. Just before launch,
the runner performs a real create/delete probe in the data root. This catches
Windows ACL/read-only failures before a host process starts; a runner-created
empty root is removed again if that probe fails. Cleanup retries transient
`PermissionError`/sharing violations five times, rechecking directory identity
before each attempt, and reports the attempt count and error type without
recording a path or command output.

The report contains phase status, health status, durations, exit codes, error
categories, version metadata, proxy mode, and an account fingerprint. It never
stores command output, media, credentials, or full upstream responses. This
runner is a prerequisite for real-host acceptance; local unit tests do not
count as a host rehearsal.

The offline contract tests also exercise UTF-8 names and a deep path beyond
the legacy 260-character Windows boundary, plus injected read-only and
transient/permanent file-lock failures. These tests validate runner behavior
only; they do not prove long-path policy, ACLs, network drives, SQLite WAL
locking, AstrBot startup, or the embedded Dashboard iframe on Windows. A real
acceptance run must still record the Windows build, long-path policy, AstrBot
version, isolated data root, and browser/iframe checks separately.

The repository's local browser fixture can exercise the embedded-page contract
without an AstrBot process:

```sh
node scripts/dashboard_content_smoke.js
```

It checks the iframe bridge path, hidden token login, bridge failure state and
desktop/mobile layout. Because its bridge and API are fixtures, a passing run
is only a browser regression signal; it is not evidence that an AstrBot host
or Windows WebView accepted the page.

## Input Contracts

`scripts/check_release.py` can run the data and artifact checks independently.
It compares the exact current version in `core/constants.py`, `metadata.yaml`,
the README stable-version declaration, and the first CHANGELOG release heading.
Historical version mentions do not satisfy the current-version check.

Every Git-tracked JSON file must parse without duplicate object keys or
non-finite numbers. Bundled configuration and prompt data also require their
supported schema version and the fields used by the runtime readers. COS
catalog references must name tracked files with matching counts and categories;
catalog identities must be unique. The existing partial legacy display order
remains supported.

## Artifact Contents

`scripts/release_checks.py` owns the runtime allowlist and explicit adoption
list for new runtime modules awaiting their first commit. Inputs come from
`git ls-files` plus that named adoption list, using current worktree contents.
Unrelated untracked files are never discovered or included automatically.
New local Python imports must resolve to a module included in the artifact.

The archive contains one plugin directory with Python runtime modules, bundled
prompt JSON, metadata, runtime requirements, README/CHANGELOG, the root logo,
and tracked image/font assets. Help cards are generated with Pillow into the
plugin data directory on first use. The generated Dashboard `index.html` and Flask
`webui/index_fallback.py` are both runtime entrypoints and each appears once.
Dashboard source fragments, tests, development scripts, CI files, design
previews, research folders, caches, and local configuration are excluded.

Size budgets are 2 MiB per file, 12 MiB uncompressed, and 6 MiB for the ZIP.
The checker emits the actual sizes, file count, largest file, version and SHA256.
It verifies the archive member list, CRCs and bytes against the selected source
snapshot. Fixed timestamps and file modes make identical source snapshots
produce identical ZIP bytes. Changing an intentional size budget or adopting a
new runtime input requires a reviewable source change.

The gates do not require AstrBot, credentials or live provider access. Local
validation proves the current interpreter and Node environment; the Python
3.10-3.13 matrix still requires an actual CI run to establish its results.
