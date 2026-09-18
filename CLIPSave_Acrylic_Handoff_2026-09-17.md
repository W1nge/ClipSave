# ClipSave Acrylic Backend Handoff

## Final architecture

Win11: DWM System Acrylic.

Win10: Windows.UI.Composition HostBackdrop + non-topmost DesktopWindowTarget + NativeAOT bridge + ctypes from PySide.

## Root cause

Old Win10 AccentPolicy Acrylic (ACCENT_ENABLE_ACRYLICBLURBEHIND=4) was rejected by benchmark:
- move ~13.77ms
- resize ~20.85ms

Win10 Composition HostBackdrop final release:
- final validation round A: move 6.943ms mean, resize 7.760ms mean
- final validation round B: move 6.971ms mean, resize 6.981ms mean
- neither final round had a sample above 33.3ms

An earlier `DesktopAttachedSiteBridge -> ContentIsland -> DesktopAcrylicController`
prototype was rejected after a real desktop screenshot showed the ContentIsland
covering all Qt controls. Do not restore that architecture even though its geometry
benchmark looked fast.

## Completed

- NativeAOT bridge integrated
- direct NativeAOT COM ABI for `ICompositorDesktopInterop`
- Windows App SDK dependency/runtime removed; packaged backdrop runtime is one DLL
- manifest integration
- fallback logic
- build pipeline integration
- release visual-smoke validation of the final desktop-composited frame
- backend diagnostics
- full 530-test regression, including the new visual-smoke analyzer tests
- Windows cmd.exe/build.bat subprocess decoding cleanup
- packaged EXE move/resize performance verification

Verified release:
- `backdrop_backend=win10_composition_acrylic`
- `backdrop_success=True`
- `backdrop_native_error=None`
- `event_loop_exited=0`

Final packaged EXE geometry validation on Win10 19044 / 144.001 Hz:

Round A:
- move: mean 6.943ms, p50 6.931ms, p95 7.172ms, p99 9.160ms, 0/240 >16.7ms, 0/240 >33.3ms
- resize: mean 7.760ms, p50 6.976ms, p95 13.845ms, p99 22.003ms, 6/240 >16.7ms, 0/240 >33.3ms

Round B:
- move: mean 6.971ms, p50 6.934ms, p95 7.179ms, p99 9.828ms, 0/240 >16.7ms, 0/240 >33.3ms
- resize: mean 6.981ms, p50 6.913ms, p95 8.534ms, p99 11.036ms, 0/240 >16.7ms, 0/240 >33.3ms

Post-cleanup final run (after failure-path hardening and the final clean rebuild):
- move: mean 6.942ms, p50 6.926ms, p95 7.209ms, p99 9.140ms, 0/240 >16.7ms, 0/240 >33.3ms
- resize: mean 7.058ms, p50 6.933ms, p95 8.795ms, p99 13.365ms, 0/240 >16.7ms, 0/240 >33.3ms

Visual regression evidence for the original blank-window bug:
- broken ContentIsland build: 105 sampled colors / 41 edge pixels
- final clean-build HostBackdrop release: 530 sampled colors / 914 edge pixels
- `visual_smoke=PASS backend=win10_composition_acrylic`

Final regression on the exact final working tree:
- `Ran 530 tests in 195.801s`
- `OK`
- no `_readerthread` / `UnicodeDecodeError` noise

Final package audit:
- `_internal/windows_backdrop/` contains only `clipsave_windows_backdrop.dll`
- no Windows App SDK / WindowsAppRuntime DLLs or license are bundled
- bridge DLL has no `Microsoft.WindowsAppRuntime.dll` reference
- embedded EXE manifest retains `asInvoker`, Windows supportedOS declarations,
  Common Controls v6 and `longPathAware`, with no private WinAppSDK registration

## Resolved test-runner issue

The apparent unittest hangs were Windows subprocess decode failures: this Python runs with UTF-8 mode enabled while `cmd.exe` emits the local Windows code page. The affected tests now use `locale.getencoding()` plus `errors="replace"` for captured `cmd.exe` / `build.bat` text. On this machine that resolves to `cp936`. Do not switch those subprocess captures back to implicit UTF-8 decoding.

## Remaining

- no Acrylic/backend blocker is known
- keep the normal release/build validation when future production code changes

## Do not revisit

Do not return to AccentPolicy Acrylic tuning. It was benchmarked and is not the correct path.
