using System.Runtime.InteropServices;
using System.Numerics;
using Microsoft.Graphics.Canvas.Effects;
using WinRT;
using Windows.UI.Composition;
using Windows.UI.Composition.Desktop;

namespace ClipSave.WindowsBackdrop;

internal static class AcrylicBridge
{
    private const int WcaAccentPolicy = 19;
    private const int AccentDisabled = 0;
    private const int AccentEnableHostBackdrop = 5;

    [StructLayout(LayoutKind.Sequential)]
    private struct AccentPolicy
    {
        public int AccentState;
        public int AccentFlags;
        public uint GradientColor;
        public int AnimationId;
    }

    private static unsafe bool SetHostBackdropEnabled(nint hwnd, bool enabled)
    {
        AccentPolicy policy = new()
        {
            AccentState = enabled ? AccentEnableHostBackdrop : AccentDisabled,
            AccentFlags = 0,
            GradientColor = 0,
            AnimationId = 0,
        };
        WindowCompositionAttribData data = new()
        {
            Attribute = WcaAccentPolicy,
            Data = (nint)(&policy),
            Size = (nuint)sizeof(AccentPolicy),
        };
        return SetWindowCompositionAttribute(hwnd, ref data) != 0;
    }

    private static Windows.UI.Color TintColor(bool dark)
    {
        return dark
            ? Windows.UI.Color.FromArgb(0x58, 0x18, 0x1B, 0x20)
            : Windows.UI.Color.FromArgb(0x48, 0xF6, 0xF7, 0xF9);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WindowCompositionAttribData
    {
        public int Attribute;
        public nint Data;
        public nuint Size;
    }

    [DllImport("user32.dll", ExactSpelling = true, SetLastError = true)]
    private static extern int SetWindowCompositionAttribute(
        nint hwnd,
        ref WindowCompositionAttribData data);

    private static readonly Guid CompositorDesktopInteropIid =
        new("29E691FA-4567-4DCA-B319-D0F207EB6807");

    private static Windows.System.DispatcherQueueController? _systemDispatcher;
    private static Compositor? _compositor;
    private static DesktopWindowTarget? _desktopTarget;
    private static ContainerVisual? _root;
    private static SpriteVisual? _backdropVisual;
    private static SpriteVisual? _tintVisual;
    private static CompositionBackdropBrush? _hostBackdropBrush;
    private static CompositionEffectFactory? _blurEffectFactory;
    private static CompositionEffectBrush? _blurBrush;
    private static CompositionColorBrush? _tintBrush;
    private static nint _attachedHwnd;
    private static int _lastError;
    private static int _lastStage;

    internal static int LastError => _lastError;
    internal static int LastStage => _lastStage;

    private static int Failure(Exception exception)
    {
        _lastError = exception.HResult;
        return 0;
    }

    internal static int IsSupported()
    {
        _lastError = 0;
        return OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763) ? 1 : 0;
    }

    private static void EnsureThreadRuntime()
    {
        _systemDispatcher ??= CoreMessagingHelper.CreateForCurrentThread();
        _compositor ??= new Compositor();
    }

    private static unsafe uint ReleaseComPointer(nint pointer)
    {
        if (pointer == 0)
        {
            return 0;
        }

        nint* vtable = *(nint**)pointer;
        var release = (delegate* unmanaged[Stdcall]<nint, uint>)vtable[2];
        return release(pointer);
    }

    private static unsafe DesktopWindowTarget CreateDesktopTarget(nint hwnd)
    {
        // C#/WinRT's custom ComImport projection (`compositor.As<TInterop>()`)
        // throws NotSupportedException under NativeAOT.  Use the COM ABI
        // directly instead: query ICompositorDesktopInterop and invoke its
        // CreateDesktopWindowTarget vtable slot.  The projected compositor
        // remains alive for the duration of this call.
        _lastStage = 31;
        nint compositorPointer = ((IWinRTObject)_compositor!).NativeObject.ThisPtr;
        nint interopPointer = 0;
        Guid iid = CompositorDesktopInteropIid;
        nint* compositorVtable = *(nint**)compositorPointer;
        var queryInterface =
            (delegate* unmanaged[Stdcall]<nint, Guid*, nint*, int>)compositorVtable[0];
        int hr = queryInterface(compositorPointer, &iid, &interopPointer);
        Marshal.ThrowExceptionForHR(hr);

        try
        {
            _lastStage = 32;
            nint* interopVtable = *(nint**)interopPointer;
            var createDesktopWindowTarget =
                (delegate* unmanaged[Stdcall]<nint, nint, int, nint*, int>)interopVtable[3];
            nint rawTarget = 0;
            // Keep the Composition target non-topmost. The dedicated backdrop
            // HWND itself sits immediately behind the foreground Qt window, so
            // the effect remains visible without entering Qt's topmost client
            // composition path (which can trigger fatal user-callback failures
            // during helper-window lifecycle changes).
            hr = createDesktopWindowTarget(interopPointer, hwnd, 0, &rawTarget);
            Marshal.ThrowExceptionForHR(hr);
            if (rawTarget == 0)
            {
                throw new COMException("CreateDesktopWindowTarget returned a null target.");
            }

            try
            {
                _lastStage = 33;
                return DesktopWindowTarget.FromAbi(rawTarget);
            }
            finally
            {
                ReleaseComPointer(rawTarget);
            }
        }
        finally
        {
            if (interopPointer != 0)
            {
                ReleaseComPointer(interopPointer);
            }
        }
    }

    internal static int Attach(nint hwnd, bool dark)
    {
        if (hwnd == 0)
        {
            _lastError = unchecked((int)0x80070057); // E_INVALIDARG
            return 0;
        }

        try
        {
            _lastError = 0;
            _lastStage = 1;
            if (IsSupported() == 0)
            {
                _lastError = unchecked((int)0x80004001); // E_NOTIMPL
                return 0;
            }

            if (_attachedHwnd == hwnd && _desktopTarget is not null)
            {
                return SetTheme(dark);
            }

            Detach();
            _lastStage = 2;
            EnsureThreadRuntime();
            _lastStage = 3;

            if (!SetHostBackdropEnabled(hwnd, true))
            {
                _lastError = Marshal.GetLastWin32Error();
                return 0;
            }
            // Record the HWND immediately after state 5 is enabled so a
            // failure in any subsequent composition setup step can reliably
            // roll the window back to AccentDisabled.
            _attachedHwnd = hwnd;

            _lastStage = 4;
            _desktopTarget = CreateDesktopTarget(hwnd);
            _root = _compositor!.CreateContainerVisual();
            _root.RelativeSizeAdjustment = Vector2.One;
            _desktopTarget.Root = _root;
            _lastStage = 5;

            _hostBackdropBrush = _compositor.CreateHostBackdropBrush();
            GaussianBlurEffect blurEffect = new()
            {
                Name = "BackdropBlur",
                BlurAmount = 20.0f,
                BorderMode = EffectBorderMode.Hard,
                Optimization = EffectOptimization.Speed,
                Source = new CompositionEffectSourceParameter("Backdrop"),
            };
            SaturationEffect acrylicEffect = new()
            {
                Name = "BackdropSaturation",
                Saturation = 1.25f,
                Source = blurEffect,
            };
            _blurEffectFactory = _compositor.CreateEffectFactory(acrylicEffect);
            _blurBrush = _blurEffectFactory.CreateBrush();
            _blurBrush.SetSourceParameter("Backdrop", _hostBackdropBrush);
            _backdropVisual = _compositor.CreateSpriteVisual();
            _backdropVisual.RelativeSizeAdjustment = Vector2.One;
            _backdropVisual.Brush = _blurBrush;
            _root.Children.InsertAtBottom(_backdropVisual);

            _tintBrush = _compositor.CreateColorBrush(TintColor(dark));
            _tintVisual = _compositor.CreateSpriteVisual();
            _tintVisual.RelativeSizeAdjustment = Vector2.One;
            _tintVisual.Brush = _tintBrush;
            _root.Children.InsertAtTop(_tintVisual);

            _lastStage = 6;
            return 1;
        }
        catch (Exception exception)
        {
            try
            {
                Detach();
            }
            catch
            {
                // Preserve the original attach failure.
            }
            return Failure(exception);
        }
    }

    internal static int SetTheme(bool dark)
    {
        try
        {
            _lastError = 0;
            if (_tintBrush is null)
            {
                _lastError = unchecked((int)0x8000FFFF); // E_UNEXPECTED
                return 0;
            }

            _tintBrush.Color = TintColor(dark);
            return 1;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }

    internal static int SetInputActive(bool active)
    {
        // HostBackdropBrush itself does not require an activation state.
        // Keep the exported contract so the Python layer can use the same API
        // on Windows 10 and Windows 11.
        _lastError = 0;
        return 1;
    }

    internal static int Detach()
    {
        nint hwnd = _attachedHwnd;
        try
        {
            _lastError = 0;
            _attachedHwnd = 0;
            try
            {
                if (_tintVisual is not null)
                {
                    _tintVisual.Brush = null;
                    _tintVisual = null;
                }
                _tintBrush = null;

                if (_backdropVisual is not null)
                {
                    _backdropVisual.Brush = null;
                    _backdropVisual = null;
                }
                _blurBrush = null;
                _blurEffectFactory = null;
                _hostBackdropBrush = null;

                if (_desktopTarget is not null)
                {
                    _desktopTarget.Root = null;
                    _desktopTarget.Dispose();
                    _desktopTarget = null;
                }
                _root = null;
            }
            finally
            {
                // Even if a composition object throws while being torn down,
                // never leave ACCENT_ENABLE_HOSTBACKDROP active on the HWND.
                if (hwnd != 0 && !SetHostBackdropEnabled(hwnd, false))
                {
                    int win32Error = Marshal.GetLastWin32Error();
                    if (win32Error != 0)
                    {
                        _lastError = win32Error;
                    }
                }
            }

            return _lastError == 0 ? 1 : 0;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }
}
