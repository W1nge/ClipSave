using Microsoft.UI.Content;
using Microsoft.UI.Composition;
using Microsoft.UI.Composition.SystemBackdrops;

namespace ClipSave.WindowsBackdrop;

internal static class AcrylicBridge
{
    private static Windows.System.DispatcherQueueController? _systemDispatcher;
    private static Microsoft.UI.Dispatching.DispatcherQueueController? _appDispatcher;
    private static Compositor? _compositor;
    private static ContainerVisual? _root;
    private static ContentIsland? _island;
    private static DesktopAttachedSiteBridge? _siteBridge;
    private static SystemBackdropConfiguration? _configuration;
    private static DesktopAcrylicController? _acrylic;
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
        try
        {
            _lastError = 0;
            return DesktopAcrylicController.IsSupported() ? 1 : 0;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }

    private static void EnsureThreadRuntime()
    {
        _systemDispatcher ??= CoreMessagingHelper.CreateForCurrentThread();
        _appDispatcher ??= Microsoft.UI.Dispatching.DispatcherQueueController.CreateOnCurrentThread();
        _compositor ??= new Compositor();
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
            if (!DesktopAcrylicController.IsSupported())
            {
                _lastError = unchecked((int)0x80004001); // E_NOTIMPL
                return 0;
            }

            if (_attachedHwnd == hwnd && _acrylic is not null)
            {
                return SetTheme(dark);
            }

            Detach();
            _lastStage = 2;
            EnsureThreadRuntime();
            _lastStage = 3;
            _root = _compositor!.CreateContainerVisual();
            _lastStage = 4;
            _island = ContentIsland.Create(_root);
            var windowId = new Microsoft.UI.WindowId((ulong)hwnd);
            _lastStage = 5;
            _siteBridge = DesktopAttachedSiteBridge.CreateFromWindowId(
                _appDispatcher!.DispatcherQueue,
                windowId);
            _lastStage = 6;
            _siteBridge.Connect(_island);
            _lastStage = 7;
            _configuration = new SystemBackdropConfiguration
            {
                IsInputActive = true,
                Theme = dark ? SystemBackdropTheme.Dark : SystemBackdropTheme.Light,
            };
            _lastStage = 8;
            _acrylic = new DesktopAcrylicController();
            _acrylic.SetSystemBackdropConfiguration(_configuration);
            _lastStage = 9;
            if (!_acrylic.AddSystemBackdropTarget(_island))
            {
                _lastError = unchecked((int)0x80004005); // E_FAIL
                Detach();
                return 0;
            }

            _attachedHwnd = hwnd;
            _lastStage = 10;
            return 1;
        }
        catch (Exception exception)
        {
            Detach();
            return Failure(exception);
        }
    }

    internal static int SetTheme(bool dark)
    {
        try
        {
            _lastError = 0;
            if (_configuration is null)
            {
                _lastError = unchecked((int)0x8000FFFF); // E_UNEXPECTED
                return 0;
            }

            _configuration.Theme = dark ? SystemBackdropTheme.Dark : SystemBackdropTheme.Light;
            return 1;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }

    internal static int SetInputActive(bool active)
    {
        try
        {
            _lastError = 0;
            if (_configuration is null)
            {
                _lastError = unchecked((int)0x8000FFFF); // E_UNEXPECTED
                return 0;
            }

            _configuration.IsInputActive = active;
            return 1;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }

    internal static int Detach()
    {
        try
        {
            _lastError = 0;
            _attachedHwnd = 0;
            if (_acrylic is not null)
            {
                _acrylic.RemoveAllSystemBackdropTargets();
                _acrylic.Dispose();
                _acrylic = null;
            }

            _configuration = null;
            _siteBridge?.Dispose();
            _siteBridge = null;
            _island = null;
            _root = null;
            return 1;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }
}
