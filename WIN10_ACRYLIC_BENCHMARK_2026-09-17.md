# Win10 Acrylic Performance Benchmark

Date: 2026-09-17

Environment: Windows 10 build 19044, 144.001 Hz desktop, PySide6/Qt window with `WA_TranslucentBackground`.

## Minimal-window A/B/C

Each case used the same visible HWND and Qt surface. The only changed variable was the native backdrop backend. Every geometry update processed Qt events and then called `DwmFlush()` so the timing includes compositor completion rather than only the API call.

Aggregate over three rotated rounds, 540 measured samples per case:

| Backend | Move mean | Resize mean |
| --- | ---: | ---: |
| Solid (`AccentState=0`) | 6.942 ms | 7.007 ms |
| Legacy Blur (`AccentState=3`) | 6.943 ms | 7.098 ms |
| Accent Acrylic (`AccentState=4`) | 13.773 ms | 20.845 ms |

At 144 Hz one refresh interval is about 6.944 ms. The measured cadences therefore line up closely with about 144 Hz for Solid/Legacy Blur, about 72 Hz for Acrylic movement, and about 48 Hz for Acrylic resize.

## Additional controls

- Static repaint without geometry changes: Solid 6.944 ms, Blur 6.954 ms, Acrylic 6.943 ms.
- Acrylic movement was essentially invariant with window size: 480x320 = 13.888 ms, 960x620 = 13.889 ms, 1500x900 = 13.887 ms.
- Wall/CPU timing: Solid move 6.939/2.214 ms; Acrylic move 13.191/1.628 ms. Solid resize 6.943/2.734 ms; Acrylic resize 20.831/5.924 ms. Most added wall time is compositor wait rather than Python/Qt CPU work.
- Native Acrylic `GradientColor` alpha was swept through 1, 32, 76, 128, 204, and 255. Movement remained about 13.65 ms and resize about 20.83 ms for every alpha.
- A real ClipSave `MainWindow` was also tested using a temporary database and temporary settings. Legacy Blur resize was about 9 ms while Accent Acrylic remained about 20.83 ms.

## Win10 Composition HostBackdrop fast path

An intermediate prototype used `DesktopAttachedSiteBridge -> ContentIsland -> DesktopAcrylicController`. It benchmarked near one 144 Hz refresh interval, but that result was not a valid final UX verification: a later real-screen capture showed the ContentIsland sitting above the Qt client content, leaving the user with one large Acrylic surface and no visible controls. That prototype is rejected.

`DesktopAcrylicController.SetTarget(WindowId, DesktopWindowTarget)` was also investigated. On Windows 10 build 19044 its documented `DWMWA_USE_HOSTBACKDROPBRUSH` prerequisite is not available, so that is not the production Win10 route either.

The composition fast path uses only OS-provided Windows composition APIs:

```text
Qt translucent HWND
  -> ACCENT_ENABLE_HOSTBACKDROP (state 5)
  -> Windows.UI.Composition Compositor
  -> non-topmost DesktopWindowTarget
  -> CreateHostBackdropBrush()
  -> tint SpriteVisual
  -> Qt client content remains above the backdrop visual
```

The composition objects live in a small in-process NativeAOT shared library called from PySide/Python with `ctypes`. C#/WinRT custom `ComImport` projection is not used for `ICompositorDesktopInterop`, because that projection throws `NotSupportedException` under NativeAOT; the bridge performs the COM `QueryInterface`/vtable call directly.

The bridge no longer depends on or bundles Microsoft Windows App SDK. A clean NativeAOT publish contains only `clipsave_windows_backdrop.dll` (plus the build-only PDB outside the release runtime).

Important correction after real-user visual inspection: HostBackdropBrush plus tint alone is not Acrylic. It samples/translucently shows the background but does not provide the native Acrylic blur/noise recipe. The user's screenshot showed the desktop sharply through the sidebar/top bar, which invalidated the earlier conclusion that this could be the resting Win10 material.

### Visual regression that found the ContentIsland bug

The final desktop-composited HWND is now captured from the screen instead of trusting `window.isVisible()` alone. On the same 1440x880 window:

| Build | Sampled colors | Edge pixels >= 24 | Result |
| --- | ---: | ---: | --- |
| Broken ContentIsland prototype | 105 | 41 | Qt UI hidden by Acrylic surface |
| Composition HostBackdrop candidate | 484 | 850 | Qt controls/content visible |
| Final clean-build HostBackdrop release | 530 | 914 | visual smoke PASS |

`verify_windows_visual_smoke.py` keeps this check as a local Windows release gate so a backdrop layer cannot silently cover the UI again.

### Final packaged ClipSave: hybrid Acrylic

The actual `build/release/ClipSave/ClipSave.exe` was then launched in smoke mode. The application itself reported:

```text
backdrop_backend=win10_native_acrylic
backdrop_success=True
backdrop_native_error=None
```

The final design is hybrid:

```text
resting HWND
  -> ACCENT_ENABLE_ACRYLICBLURBEHIND (state 4)
  -> real Win10 Acrylic

WM_ENTERSIZEMOVE
  -> disable state 4
  -> attach fast non-topmost HostBackdrop composition

WM_EXITSIZEMOVE
  -> detach fast composition
  -> restore state 4 immediately
```

This keeps the visually correct material while stationary, but avoids the state-4 72/48 Hz compositor cadence only during live geometry changes.

The packaged EXE was exercised with 240 moves and 240 resizes while explicitly entering/exiting the interactive size/move loop:

| Action | Mean | p50 | p95 | p99 | >16.7 ms | >33.3 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Move | 6.918 ms | 6.936 ms | 7.091 ms | 7.502 ms | 0/240 | 0/240 |
| Resize | 7.436 ms | 7.272 ms | 10.328 ms | 23.399 ms | 4/240 | 0/240 |

The final visual smoke of the packaged hybrid release reported 1360 sampled colors and 614 edge pixels, with `visual_smoke=PASS backend=win10_native_acrylic`. The old HostBackdrop-only release measured 530 sampled colors / 914 edge pixels. The metric is used only as a regression guard that Qt UI remains visible; material correctness comes from the native state-4 resting path rather than from this scalar image score.

The exact final Python working tree was also run through the complete unittest suite: 531/531 tests passed in 324.968 seconds.

The final clean release package was audited as well. `_internal/windows_backdrop/` contains only `clipsave_windows_backdrop.dll`; neither the release directory nor the portable ZIP contains Windows App SDK / WindowsAppRuntime files, and the NativeAOT bridge no longer references `Microsoft.WindowsAppRuntime.dll`. The embedded executable manifest still contains the normal `asInvoker`, supportedOS, Common Controls v6 and `longPathAware` declarations without any private WinAppSDK registration.

## Final conclusion

The Win10 `ACCENT_ENABLE_ACRYLICBLURBEHIND` path is the correct resting material, but it exhibits a lower compositor cadence during live move/resize on this machine. The evidence does not support ClipSave UI complexity, `WA_TranslucentBackground`, window area, tint alpha, or Python/Qt CPU cost as the primary cause.

The final Win10 solution is therefore not one backend for all phases. It uses native state-4 Acrylic at rest and the fast Windows.UI.Composition HostBackdrop only as a transient interactive fallback. Legacy `ACCENT_ENABLE_BLURBEHIND = 3` remains a compatibility fallback if the fast composition bridge cannot be attached.
