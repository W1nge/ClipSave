# ClipSave Release Checklist

Use this checklist before publishing a tagged Windows release.

## Automated checks

The normal test and packaging workflow must pass (CI runs the first two on
every push; the whitespace check is a local pre-tag step):

```text
python -m unittest discover -s tests -v
python -m compileall -q clipsave.py clipsave_app tests
git diff --check
```

Build the Windows backdrop and the release package from a clean source tree.

## Replacing the withdrawn 1.1.5 draft

The withdrawn `v1.1.5` originally pointed to `5a04f5a`, before the resize and UI
repairs. Its old draft assets must not be published again. Ordinary release runs
skip existing releases. To rebuild an unpublished draft, explicitly dispatch the
release workflow with `tag=v1.1.5` and `replace_draft=true` after updating the tag
to the reviewed commit. The workflow refuses to replace a published release and
keeps the rebuilt release as a draft for desktop acceptance.

Before publishing the replacement, commit the reviewed changes, deliberately
update the withdrawn tag/draft through the release process, and build fresh
official artifacts. Verify that the tag's commit, packaged `BUILD_INFO.txt`,
and reviewed source commit agree; verify both new asset checksums. Run the gates
below against those exact new artifacts. Previous desktop results for `5a04f5a`
do not validate the repaired release.

## Interactive Windows visual gates

Run these gates on an interactive Windows desktop with DWM enabled. They are kept
as explicit release gates rather than GitHub-hosted CI performance checks because
compositor cadence and desktop-session behavior are environment-sensitive.

After building the release executable:

```text
.venv\Scripts\python.exe verify_windows_visual_smoke.py --exe build\release\ClipSave\ClipSave.exe --expected-backend win10_effect_acrylic
.venv\Scripts\python.exe verify_windows_interactive_backdrop.py --count 240 --timeout 60 -- build\release\ClipSave\ClipSave.exe
```

Both commands must pass. The interactive gate must also report:

- `resting_backend=win10_effect_acrylic`
- `success=True`
- `geometry_lock: max_delta=0px`

For `single_host_cached`, geometry samples query the native composition tree:
the background and foreground must share the root clip and relative bounds.
The gate also requires new cached frames during resizing and zero callback errors.

Do not publish a release when either visual gate is skipped or failing.

These synthetic gates are necessary but do not establish interactive UI quality.
The geometry timing measures SetWindowPos/DwmFlush, not fresh Qt frame cadence.
Verify a populated library through the normal application entry point and real
continuous corner dragging: cards must reflow during the drag, without showing a
stretched old interface until release. Run the native composition regression with
QT_QPA_PLATFORM=windows; it must produce new frames and layout sizes while resize
messages continue faster than the presentation timer. An end-of-drag frame alone
does not pass. Obtain user acceptance of this repaired integration before publishing.

The tag workflow uploads a **draft** release. Download its exact assets, run both
desktop gates above, and only then publish the draft as the latest release.
