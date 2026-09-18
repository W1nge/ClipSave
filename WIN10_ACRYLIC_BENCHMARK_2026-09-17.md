# Win10 Acrylic Performance Benchmark

Updated: 2026-09-18

Environment:

- Windows 10 build 19044
- 144.001 Hz desktop
- PySide6 / Qt translucent top-level window

## Historical AccentPolicy baseline

The old native AccentPolicy paths were measured with the same visible HWND and
Qt surface. Each geometry update processed Qt events and then called DwmFlush().

| Backend | Move mean | Resize mean |
| --- | ---: | ---: |
| Solid, AccentState=0 | 6.942 ms | 7.007 ms |
| Legacy Blur, AccentState=3 | 6.943 ms | 7.098 ms |
| Native Acrylic, AccentState=4 | 13.773 ms | 20.845 ms |

At 144 Hz one refresh interval is about 6.944 ms. The state-4 measurements
therefore correspond roughly to 72 Hz movement and 48 Hz resize cadence.

Additional controls established that the slowdown was compositor-side:

- static repaint without geometry changes stayed near 6.94 ms;
- Acrylic move time was nearly invariant with window size;
- changing Accent Acrylic GradientColor alpha did not materially change move or resize cadence;
- CPU time did not account for the extra wall-clock delay.

Conclusion: AccentState=4 is visually correct Acrylic, but it is not suitable
for live geometry changes on this validation host.

## Failed intermediate paths

### ContentIsland / DesktopAcrylicController

DesktopAttachedSiteBridge -> ContentIsland -> DesktopAcrylicController could
benchmark quickly, but a real desktop capture showed the ContentIsland covering
the Qt client content. The window became one large Acrylic surface with the Qt
controls hidden. This architecture is rejected.

### HostBackdrop plus tint only

HostBackdropBrush with a tint visual benchmarked near a 144 Hz refresh
interval, but the user's real screenshot showed the desktop sharply through the
window. That path was translucent, not Acrylic.

## Decisive Composition bug

The NativeAOT bridge later added a real GaussianBlurEffect, but initial tests
still appeared sharp even though the bridge returned attach=True.

The root cause was the Composition root ContainerVisual:

~~~csharp
_root = _compositor.CreateContainerVisual();
_desktopTarget.Root = _root;
~~~

All child visuals used RelativeSizeAdjustment = Vector2.One, but the root itself
had no size. The effect tree therefore existed successfully while effectively
rendering at 0x0.

The required fix is:

~~~csharp
_root = _compositor.CreateContainerVisual();
_root.RelativeSizeAdjustment = Vector2.One;
_desktopTarget.Root = _root;
~~~

After this change the controlled stripe probe immediately showed strong blur.

## Final Win10 Composition Acrylic

The final effect path is:

~~~text
pure Win32 backdrop HWND
  -> DesktopWindowTarget
  -> HostBackdropBrush
  -> GaussianBlurEffect
  -> tint SpriteVisual

foreground Qt HWND
  -> all application UI
~~~

The backdrop HWND is:

- WS_POPUP
- WS_EX_TOOLWINDOW
- WS_EX_NOACTIVATE
- WS_EX_TRANSPARENT
- WS_EX_NOREDIRECTIONBITMAP

It is not Qt-owned. This matters because SetWindowPos can then be called from
the host window's WM_WINDOWPOSCHANGING callback without re-entering Qt's QWindow
state machine.

The final implementation keeps the same blur material active continuously.
There is no material swap on WM_ENTERSIZEMOVE or WM_EXITSIZEMOVE.

## Controlled packaged blur verification

The final packaged ClipSave.exe was placed above a 6-pixel alternating
black/white stripe field.

| Sample | Adjacent-pixel mean |
| --- | ---: |
| Outside window | 42.194 |
| Resting Acrylic | 4.089 |
| Interactive Acrylic | 4.089 |

Ratios:

~~~text
resting_ratio     = 0.0969
interactive_ratio = 0.0969
interactive_blur  = PASS
~~~

The static and WM_ENTERSIZEMOVE measurements are identical to three decimal
places. The material therefore remains blurred during the interactive loop.

## Final packaged performance

Target:

~~~text
build/release/ClipSave/ClipSave.exe
~~~

The verifier ran 240 move samples and 240 resize samples while explicitly
entering the real interactive size/move loop.

| Action | Mean | p50 | p95 | p99 | >16.7 ms | >33.3 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Move | 6.928 ms | 6.936 ms | 7.226 ms | 8.348 ms | 0/240 | 0/240 |
| Resize | 9.337 ms | 8.408 ms | 14.119 ms | 23.977 ms | 6/240 | 0/240 |

Geometry lock was measured independently:

~~~text
initial RECT delta = 0 px
move max delta      = 0 px
resize max delta    = 0 px
~~~

The final 1-pixel resize discrepancy was removed by:

1. following the proposed RECT in WM_WINDOWPOSCHANGING;
2. snapping to the committed GetWindowRect in WM_WINDOWPOSCHANGED;
3. using geometry-only SetWindowPos with SWP_NOZORDER inside live callbacks.

## Packaged visual smoke

~~~text
visual_smoke=PASS
backend=win10_effect_acrylic
unique=718/19800
edge=519/19800
edge_mean=1.813
rgb_stddev=11.562,12.180,13.278
~~~

The visual-smoke metric is a UI visibility regression guard. Material
correctness is established by the controlled stripe test above.

## NativeAOT and dependencies

The bridge remains an in-process NativeAOT shared library.

Win2D provides the Gaussian blur effect projection/runtime. It is consumed using
NuGet PackageDownload so its XAML framework references are not imported into the
bridge build.

Final runtime files:

- clipsave_windows_backdrop.dll
- Microsoft.Graphics.Canvas.dll
- msvcp140_app.dll
- vcruntime140_1_app.dll
- vcruntime140_app.dll

The Win2D and VCRT Forwarders license texts are bundled with the release.

## Final regression

~~~text
Ran 532 tests in 322.948s
OK
~~~

## Final conclusion

The final Win10 solution is one continuously active Composition Acrylic
material, not a hybrid backend.

The old AccentState=4 result remains useful as evidence that native Acrylic has
a geometry-change cadence limitation on this machine, but the production path
no longer needs to switch away from Acrylic during movement.

The two most important implementation constraints are:

1. the Composition root must be sized with RelativeSizeAdjustment = Vector2.One;
2. the backdrop host must be a pure Win32 HWND, not a Qt-owned helper top-level.
