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
consistency, bundled JSON contracts, a temporary runtime ZIP, compilation,
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
prompt JSON, metadata, runtime requirements, README/CHANGELOG, posters, logos,
and tracked image/font assets. The generated Dashboard `index.html` and Flask
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
