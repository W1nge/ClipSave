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

## Win10 Composition HostBackdrop Acrylic

An intermediate prototype used `DesktopAttachedSiteBridge -> ContentIsland -> DesktopAcrylicController`. It benchmarked near one 144 Hz refresh interval, but that result was not a valid final UX verification: a later real-screen capture showed the ContentIsland sitting above the Qt client content, leaving the user with one large Acrylic surface and no visible controls. That prototype is rejected.

`DesktopAcrylicController.SetTarget(WindowId, DesktopWindowTarget)` was also investigated. On Windows 10 build 19044 its documented `DWMWA_USE_HOSTBACKDROPBRUSH` prerequisite is not available, so that is not the production Win10 route either.

The corrected Win10 path uses only OS-provided Windows composition APIs:

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

### Visual regression that found the ContentIsland bug

The final desktop-composited HWND is now captured from the screen instead of trusting `window.isVisible()` alone. On the same 1440x880 window:

| Build | Sampled colors | Edge pixels >= 24 | Result |
| --- | ---: | ---: | --- |
| Broken ContentIsland prototype | 105 | 41 | Qt UI hidden by Acrylic surface |
| Composition HostBackdrop candidate | 484 | 850 | Qt controls/content visible |
| Final clean-build HostBackdrop release | 530 | 914 | visual smoke PASS |

`verify_windows_visual_smoke.py` keeps this check as a local Windows release gate so a backdrop layer cannot silently cover the UI again.

### Final packaged ClipSave

The actual `build/release/ClipSave/ClipSave.exe` was then launched in smoke mode. The application itself reported:

```text
backdrop_backend=win10_composition_acrylic
backdrop_success=True
backdrop_native_error=None
```

An external Win32 geometry driver then exercised the real packaged ClipSave HWND in three 240-move / 240-resize release-validation rounds:

| Round | Action | Mean | p50 | p95 | p99 | >16.7 ms | >33.3 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | Move | 6.943 ms | 6.931 ms | 7.172 ms | 9.160 ms | 0/240 | 0/240 |
| A | Resize | 7.760 ms | 6.976 ms | 13.845 ms | 22.003 ms | 6/240 | 0/240 |
| B | Move | 6.971 ms | 6.934 ms | 7.179 ms | 9.828 ms | 0/240 | 0/240 |
| B | Resize | 6.981 ms | 6.913 ms | 8.534 ms | 11.036 ms | 0/240 | 0/240 |
| C (post-cleanup final) | Move | 6.942 ms | 6.926 ms | 7.209 ms | 9.140 ms | 0/240 | 0/240 |
| C (post-cleanup final) | Resize | 7.058 ms | 6.933 ms | 8.795 ms | 13.365 ms | 0/240 | 0/240 |

The full application is heavier than a minimal probe, so these numbers are not a pure backend microbenchmark. They do confirm that the old 72/48 Hz AccentPolicy cadence is gone: move and the central resize distribution are again near one 144 Hz refresh interval. Round A showed several isolated resize stalls above 16.7 ms, while round B showed none; critically, none of the three final release-validation rounds contained a sample above 33.3 ms.

The exact final Python working tree was also run through the complete unittest suite after the Windows subprocess decoding cleanup and visual-smoke analyzer addition: 530/530 tests passed in 195.801 seconds with no `_readerthread` / `UnicodeDecodeError` noise.

The final clean release package was audited as well. `_internal/windows_backdrop/` contains only `clipsave_windows_backdrop.dll`; neither the release directory nor the portable ZIP contains Windows App SDK / WindowsAppRuntime files, and the NativeAOT bridge no longer references `Microsoft.WindowsAppRuntime.dll`. The embedded executable manifest still contains the normal `asInvoker`, supportedOS, Common Controls v6 and `longPathAware` declarations without any private WinAppSDK registration.

## Final conclusion

The Win10 `ACCENT_ENABLE_ACRYLICBLURBEHIND` path itself exhibits a lower compositor cadence during window move/resize on this machine. The evidence does not support ClipSave UI complexity, `WA_TranslucentBackground`, window area, tint alpha, or Python/Qt CPU cost as the primary cause.

This Win10 AccentPolicy Acrylic path therefore should not be treated as a performance-equivalent implementation of Win11 `DWMWA_SYSTEMBACKDROP_TYPE = 3`.

The final Win10 backend is the non-topmost Windows.UI.Composition HostBackdrop path described above. It preserves Qt content visibility while restoring near-refresh-rate move/resize behavior on this Win10 19044 machine. `ACCENT_ENABLE_ACRYLICBLURBEHIND = 4` is no longer used as a normal path; legacy `ACCENT_ENABLE_BLURBEHIND = 3` remains only as a compatibility fallback when the composition bridge cannot be used.
