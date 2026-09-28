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

The tag workflow uploads a **draft** release. Download its exact assets, run both
desktop gates above, and only then publish the draft as the latest release.
