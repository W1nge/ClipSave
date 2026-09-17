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

## Windows App SDK modern Acrylic

After the AccentPolicy bottleneck was isolated, a second Win10 native path was tested using Windows App SDK 1.8.260804001:

```text
Win32 HWND
  -> DesktopAttachedSiteBridge
  -> ContentIsland
  -> DesktopAcrylicController
```

The controller was hosted in-process through a NativeAOT shared library and called from PySide/Python with `ctypes`. The final deployment shape is the same one used by ClipSave: PyInstaller EXE, embedded reg-free WinRT manifest, and a private `_internal/windows_backdrop` runtime directory.

### Minimal packaged PySide probe

On the same Windows 10 build 19044 / 144.001 Hz machine:

| Action | Mean | Result |
| --- | ---: | --- |
| Move | 6.942 ms | approximately one 144 Hz refresh |
| Resize | 7.056 ms | approximately one 144 Hz refresh |

No measured sample exceeded 16.7 ms in the 240-sample minimal packaged probe.

### Final packaged ClipSave

The actual `build/release/ClipSave/ClipSave.exe` was then launched in smoke mode. The application itself reported:

```text
backdrop_backend=windows_app_sdk_acrylic
backdrop_success=True
backdrop_native_error=None
```

An external Win32 geometry driver then exercised the real packaged ClipSave HWND for 240 measured moves and 240 measured resizes:

| Action | Mean | p50 | p95 | p99 | >16.7 ms | >33.3 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Move | 6.941 ms | 6.937 ms | 7.147 ms | 7.329 ms | 0/240 | 0/240 |
| Resize | 7.177 ms | 6.850 ms | 9.155 ms | 13.665 ms | 1/240 | 0/240 |

The full application is heavier than the minimal probe, so these numbers are not a pure backend microbenchmark. They do, however, confirm that the old 72/48 Hz AccentPolicy cadence is gone in the final packaged application. In this final rerun, move was essentially one 144 Hz refresh interval and resize stayed close to it; there were no >33.3 ms stalls.

The exact final Python working tree was also run through the complete unittest suite after the Windows subprocess decoding cleanup: 528/528 tests passed in 185.250 seconds with no `_readerthread` / `UnicodeDecodeError` noise.

## Final conclusion

The Win10 `ACCENT_ENABLE_ACRYLICBLURBEHIND` path itself exhibits a lower compositor cadence during window move/resize on this machine. The evidence does not support ClipSave UI complexity, `WA_TranslucentBackground`, window area, tint alpha, or Python/Qt CPU cost as the primary cause.

This Win10 AccentPolicy Acrylic path therefore should not be treated as a performance-equivalent implementation of Win11 `DWMWA_SYSTEMBACKDROP_TYPE = 3`.

Windows App SDK `DesktopAcrylicController`, by contrast, restores modern compositor behavior on this Win10 19044 machine and is now ClipSave's primary Win10 Acrylic backend. `ACCENT_ENABLE_ACRYLICBLURBEHIND = 4` is no longer used as a normal path; legacy `ACCENT_ENABLE_BLURBEHIND = 3` remains only as a compatibility fallback when the modern controller cannot be used.
