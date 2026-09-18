# ClipSave Acrylic Backend Handoff

## Final architecture

Win11: DWM System Acrylic.

Win10: hybrid material path:
- at rest: native `ACCENT_ENABLE_ACRYLICBLURBEHIND = 4` (real Win10 Acrylic)
- during `WM_ENTERSIZEMOVE`: temporary Windows.UI.Composition HostBackdrop fast path
- on `WM_EXITSIZEMOVE`: detach the fast path and immediately restore state 4

The transient composition path uses the non-topmost DesktopWindowTarget +
NativeAOT bridge + ctypes from PySide. It is deliberately not called Acrylic:
it is a performance fallback used only while the window geometry is changing.

## Root cause

Win10 native Acrylic (ACCENT_ENABLE_ACRYLICBLURBEHIND=4) is visually correct but was rejected as a live-move/live-resize backend by benchmark:
- move ~13.77ms
- resize ~20.85ms

The previous HostBackdrop-only release fixed geometry cadence, but the user's
real screenshot proved that it was only a sharp translucent/tinted backdrop,
not Acrylic: background detail remained unblurred. That release is superseded.

The hybrid route keeps real state-4 Acrylic while stationary and switches to
the proven fast HostBackdrop composition only during interactive move/resize.

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
- full 531-test regression, including the hybrid backdrop-switch test
- Windows cmd.exe/build.bat subprocess decoding cleanup
- packaged EXE move/resize performance verification

Verified release:
- `backdrop_backend=win10_native_acrylic`
- `backdrop_success=True`
- `backdrop_native_error=None`
- `event_loop_exited=0`

Final packaged EXE interactive geometry validation on Win10 19044 / 144.001 Hz:
- move: mean 6.918ms, p50 6.936ms, p95 7.091ms, p99 7.502ms,
  0/240 >16.7ms, 0/240 >33.3ms
- resize: mean 7.436ms, p50 7.272ms, p95 10.328ms, p99 23.399ms,
  4/240 >16.7ms, 0/240 >33.3ms

Visual regression evidence for the original blank-window bug:
- broken ContentIsland build: 105 sampled colors / 41 edge pixels
- old HostBackdrop-only release: 530 sampled colors / 914 edge pixels
- hybrid native-Acrylic release: 1360 sampled colors / 614 edge pixels
- `visual_smoke=PASS backend=win10_native_acrylic`

Final regression on the exact final working tree:
- `Ran 531 tests in 324.968s`
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

- Do not use the tint-only HostBackdrop composition as the resting material; it
  is fast but is not Acrylic.
- Do not leave AccentState 4 active during live move/resize on this Win10 host;
  it is the correct resting material but has a stable lower compositor cadence
  while geometry changes.
