# Release Checks

Run every release gate before publishing:

```sh
python scripts/verify_release.py --require-node
```

`--layer python`, `--layer frontend`, and `--layer static` remain available for
focused checks. The static layer includes version consistency, bundled JSON
contracts, a temporary runtime ZIP, compilation, Dashboard checks, and
`git diff --check`. It does not run pytest. Local frontend checks may skip Node
when it is unavailable; release builds require Node explicitly.

To create an installable artifact after all gates pass:

```sh
python scripts/build_release.py --output dist/astrbot_plugin_selfie_image.zip
```

The command always runs the full gate, including pytest. There is no skip or
force option, and an existing output file is never overwritten. CI runs this
same command after its Python and frontend jobs pass, then retains the ZIP as a
workflow artifact. Creating or editing the workflow locally does not execute it.

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
