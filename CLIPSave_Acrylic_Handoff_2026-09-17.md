# ClipSave Acrylic Backend Handoff

Updated: 2026-09-18

## Final architecture

Windows 11:
- DWM System Acrylic via DWMWA_SYSTEMBACKDROP_TYPE = 3.

Windows 10:
- one always-on Windows.UI.Composition Acrylic effect for the entire visible lifetime of the window;
- no material switch on WM_ENTERSIZEMOVE / WM_EXITSIZEMOVE;
- no ACCENT_ENABLE_ACRYLICBLURBEHIND = 4 in the production path;
- no tint-only HostBackdrop masquerading as Acrylic.

The final Win10 structure is:

~~~text
desktop
  -> pure Win32 backdrop HWND
       WS_EX_TOOLWINDOW
       WS_EX_NOACTIVATE
       WS_EX_TRANSPARENT
       WS_EX_NOREDIRECTIONBITMAP
       -> DesktopWindowTarget
       -> HostBackdropBrush
       -> GaussianBlurEffect
       -> tint visual
  -> foreground Qt / PySide HWND
       -> all ClipSave UI
~~~

The backdrop HWND is kept immediately behind the Qt HWND and never accepts input or activation. During live move/resize the two Win32 rectangles are synchronized at WM_WINDOWPOSCHANGING, followed by a geometry-only final snap at WM_WINDOWPOSCHANGED.

## Root causes found

### 1. Native Accent Acrylic is visually correct but too slow during geometry changes

On Windows 10 build 19044 / 144.001 Hz:

- AccentState=4 move: about 13.77 ms
- AccentState=4 resize: about 20.85 ms

This is compositor cadence, not Python/Qt CPU time. The old state-4 path is therefore retained only as historical evidence, not as the final backend.

### 2. HostBackdrop + tint alone is not Acrylic

The earlier HostBackdrop-only implementation fixed move/resize cadence, but the user's real screenshot showed the desktop sharply through the UI. That proved that a backdrop sample plus tint is not sufficient; a real blur effect is required.

### 3. The Composition effect tree existed but rendered at 0x0

This was the decisive bug.

The bridge created a root ContainerVisual, and all child visuals used RelativeSizeAdjustment = Vector2.One, but the root itself never received a size. The effect graph could attach successfully while drawing nothing.

The fix is:

~~~csharp
_root = _compositor.CreateContainerVisual();
_root.RelativeSizeAdjustment = Vector2.One;
_desktopTarget.Root = _root;
~~~

After that fix, the controlled 6-pixel black/white stripe probe immediately changed from sharp passthrough to strong blur.

### 4. One Qt top-level HWND cannot safely host both layers

Putting the Composition tree on the same Qt-owned HWND either hides the Qt client layer or makes transparent child/native surfaces bypass the blur.

The robust design is two top-level HWNDs:

- lower pure Win32 backdrop HWND;
- upper Qt UI HWND.

The lower HWND must not be Qt-owned, because synchronously moving a second Qt-owned top-level from the host's WM_WINDOWPOSCHANGING callback can re-enter Qt's window state machine and trigger STATUS_FATAL_USER_CALLBACK_EXCEPTION (0xC000041D).

## Native bridge

Files:

- native/windows_backdrop/AcrylicBridge.cs
- native/windows_backdrop/NativeExports.cs
- native/windows_backdrop/WindowsBackdropBridge.csproj
- clipsave_app/windows_backdrop.py

The bridge is .NET 8 NativeAOT and calls ICompositorDesktopInterop through the COM ABI directly.

The Win10 effect graph currently uses:

~~~text
HostBackdropBrush
  -> GaussianBlurEffect
  -> SpriteVisual
  -> tint SpriteVisual
~~~

Win2D is used only for the effect projection/runtime. NuGet PackageDownload is used so Win2D's XAML framework references are not imported into the NativeAOT bridge project.

Required packaged runtime files:

- clipsave_windows_backdrop.dll
- Microsoft.Graphics.Canvas.dll
- msvcp140_app.dll
- vcruntime140_1_app.dll
- vcruntime140_app.dll

The three VCRT APP forwarders are required by Microsoft.Graphics.Canvas.dll.

## Window synchronization

The backdrop host is a pure Win32 WS_POPUP created by create_backdrop_host_window().

Important behavior:

- normal show / restore can synchronize geometry and z-order;
- inside live move/resize, WM_WINDOWPOSCHANGING follows the proposed RECT with sync_z_order=False;
- WM_WINDOWPOSCHANGED performs a geometry-only snap to the final committed GetWindowRect();
- no z-order manipulation is performed from the host's live window callback.

This eliminated the final occasional 1-pixel resize mismatch without reintroducing the Windows user-callback crash.

## Final packaged validation

Target:

~~~text
D:\GPT_WEB\clipsave\build\release\ClipSave\ClipSave.exe
~~~

Host:

- Windows 10 build 19044
- 144.001 Hz desktop

### Visual smoke

~~~text
visual_smoke=PASS
backend=win10_effect_acrylic

unique=718/19800
edge=519/19800
edge_mean=1.813
~~~

### Controlled blur probe

The final packaged EXE was placed above a 6-pixel black/white stripe field.

~~~text
outside_adjacent_mean     = 42.194
resting_adjacent_mean     = 4.089
interactive_adjacent_mean = 4.089

resting_ratio     = 0.0969
interactive_ratio = 0.0969
interactive_blur  = PASS
~~~

The blur strength is unchanged when entering the real WM_ENTERSIZEMOVE loop.

### Interactive performance and geometry lock

~~~text
move
  mean 6.928 ms
  p50  6.936 ms
  p95  7.226 ms
  p99  8.348 ms
  >16.7 ms = 0/240
  >33.3 ms = 0/240
  geometry max delta = 0 px

resize
  mean 9.337 ms
  p50  8.408 ms
  p95  14.119 ms
  p99  23.977 ms
  >16.7 ms = 6/240
  >33.3 ms = 0/240
  geometry max delta = 0 px
~~~

### Full regression

~~~text
Ran 532 tests in 322.948s
OK
~~~

## Packaging / licensing

The portable release includes the Win2D runtime and the required VCRT forwarders. Their license texts are included under third_party_licenses/ and listed in THIRD_PARTY_NOTICES.md.

Inno Setup is optional; on the validation host it was not installed, so the portable ZIP was built successfully without an installer.

## Do not regress

- Do not restore the old state-4 / fast-path hybrid switch.
- Do not use HostBackdrop + tint without Gaussian blur and call it Acrylic.
- Do not remove _root.RelativeSizeAdjustment = Vector2.One.
- Do not replace the pure Win32 backdrop HWND with a Qt-owned helper top-level.
- Do not change z-order from inside the host's live WM_WINDOWPOSCHANGING callback.
- Do not remove the Win2D / VCRT runtime files or their license notices from the release package.
