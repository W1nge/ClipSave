using System.Numerics;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using Microsoft.Graphics.Canvas;
using Microsoft.Graphics.Canvas.UI.Composition;
using Windows.Graphics.DirectX;
using Windows.UI.Composition;
using WinRT;

namespace ClipSave.WindowsBackdrop;

// Qt supplies premultiplied BGRA pixels. Acrylic and the foreground share one
// desktop target and root clip, independent of input-window ownership.
internal static partial class AcrylicBridge
{
    private static CanvasDevice? _canvasDevice;
    private static CompositionGraphicsDevice? _graphicsDevice;
    private static CompositionDrawingSurface? _foregroundSurface;
    private static CompositionSurfaceBrush? _foregroundBrush;
    private static SpriteVisual? _foregroundVisual;
    private static nint _uploadBitmap;
    private static int _frameWidth, _frameHeight;
    private static readonly List<RetiredFrame> _retiredFrames = new();
    private static int _releasedFrames;

    private sealed class RetiredFrame
    {
        internal required CompositionDrawingSurface Surface;
        internal required CompositionCommitBatch Batch;
        internal Windows.Foundation.TypedEventHandler<object, CompositionBatchCompletedEventArgs>? Handler;
    }

    private static void RetireAfterCommit(CompositionDrawingSurface surface)
    {
        var retired = new RetiredFrame {
            Surface = surface,
            Batch = _compositor!.GetCommitBatch(CompositionBatchTypes.Animation),
        };
        retired.Handler = (_, _) => {
            retired.Batch.Completed -= retired.Handler;
            retired.Surface.Dispose();
            _retiredFrames.Remove(retired);
            _releasedFrames++;
        };
        _retiredFrames.Add(retired);
        retired.Batch.Completed += retired.Handler;
    }

    internal static int PresentFrame(nint pixels, int width, int height, int stride)
    {
        try
        {
            _lastError = 0;
            if (_root is null || pixels == 0 || width <= 0 || height <= 0
                || width > 16384 || height > 16384 || stride < width * 4)
                throw new ArgumentException("Invalid frame buffer or unattached target.");

            _canvasDevice ??= new CanvasDevice();
            _graphicsDevice ??= CanvasComposition.CreateCompositionGraphicsDevice(_compositor!, _canvasDevice);
            if (_foregroundVisual is null)
            {
                _foregroundVisual = _compositor!.CreateSpriteVisual();
                _foregroundVisual.RelativeSizeAdjustment = Vector2.One;
                _foregroundBrush = _compositor.CreateSurfaceBrush();
                _foregroundBrush.Stretch = CompositionStretch.Fill;
                _foregroundBrush.HorizontalAlignmentRatio = 0;
                _foregroundBrush.VerticalAlignmentRatio = 0;
                _foregroundVisual.Brush = _foregroundBrush;
                _root.Children.InsertAtTop(_foregroundVisual);
                // The compositor derives both layers' bounds from the host.
                // An older foreground surface stretches to those bounds until
                // the freshly laid out pixels arrive. Never attach the scene
                // extent to the previous CPU-rendered frame's dimensions.
                _root.Size = Vector2.Zero;
                _root.RelativeSizeAdjustment = Vector2.One;
                _root.Clip = _compositor.CreateInsetClip();
            }

            bool resized = width != _frameWidth || height != _frameHeight;
            CompositionDrawingSurface? newSurface = null;
            if (resized || _foregroundSurface is null)
            {
                ReleaseComPointer(_uploadBitmap);
                _uploadBitmap = 0;
                newSurface = _graphicsDevice.CreateDrawingSurface(
                    new Windows.Foundation.Size(width, height),
                    DirectXPixelFormat.B8G8R8A8UIntNormalized, DirectXAlphaMode.Premultiplied);
            }
            var surface = newSurface ?? _foregroundSurface!;
            using (var draw = CanvasComposition.CreateDrawingSession(surface))
            {
                draw.Clear(Windows.UI.Color.FromArgb(0, 0, 0, 0));
                DrawPixels(draw, pixels, width, height, stride);
            }
            if (newSurface is not null)
            {
                var previous = _foregroundSurface;
                _foregroundSurface = newSurface;
                _foregroundBrush!.Surface = newSurface;
                _frameWidth = width;
                _frameHeight = height;
                // Surface assignment is asynchronous. Closing the previous
                // surface here can invalidate pixels still used by the scene.
                if (previous is not null) RetireAfterCommit(previous);
            }
            _ = _compositor!.RequestCommitAsync();
            return 1;
        }
        catch (Exception exception)
        {
            return Failure(exception);
        }
    }

    internal static int ReleaseFrames()
    {
        try
        {
            if (_foregroundVisual is not null) _foregroundVisual.Brush = null;
            if (_foregroundBrush is not null) _foregroundBrush.Surface = null;
            foreach (var retired in _retiredFrames)
            {
                retired.Batch.Completed -= retired.Handler;
                retired.Surface.Dispose();
            }
            _retiredFrames.Clear();
            _foregroundSurface?.Dispose();
            ReleaseComPointer(_uploadBitmap);
            _graphicsDevice?.Dispose();
            _canvasDevice?.Dispose();
            _foregroundSurface = null;
            _foregroundVisual = null;
            _foregroundBrush = null;
            _uploadBitmap = 0;
            _graphicsDevice = null;
            _canvasDevice = null;
            _frameWidth = _frameHeight = 0;
            return 1;
        }
        catch (Exception exception) { return Failure(exception); }
    }

    internal static int PendingFrames => _retiredFrames.Count;
    internal static int ReleasedFrames => _releasedFrames;

    // Read-only release-gate evidence: both layers derive their bounds from
    // the same native target. No CPU frame dimensions may drive the root.
    internal static int HasSharedFrameClip =>
        _root is not null && _foregroundVisual is not null && _backdropVisual is not null
        && _foregroundBrush is not null && _foregroundSurface is not null
        && _root.Size == Vector2.Zero && _root.RelativeSizeAdjustment == Vector2.One
        && _root.Offset == Vector3.Zero
        && _root.Clip is InsetClip { LeftInset: 0, TopInset: 0, RightInset: 0, BottomInset: 0 }
        && _foregroundVisual.Parent == _root && _backdropVisual.Parent == _root
        && _foregroundVisual.Size == Vector2.Zero && _backdropVisual.Size == Vector2.Zero
        && _foregroundVisual.RelativeSizeAdjustment == Vector2.One
        && _backdropVisual.RelativeSizeAdjustment == Vector2.One
        && _foregroundBrush.Stretch == CompositionStretch.Fill ? 1 : 0;

    internal static int SetFrameMaterial(bool dark, bool enabled)
    {
        try
        {
            _lastError = 0;
            if (_attachedHwnd == 0 || _backdropVisual is null || _tintBrush is null)
                throw new InvalidOperationException("No attached composition host.");
            if (!SetHostBackdropEnabled(_attachedHwnd, enabled))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            _backdropVisual.IsVisible = enabled;
            _tintBrush.Color = enabled ? TintColor(dark) : dark
                ? Windows.UI.Color.FromArgb(255, 24, 27, 32)
                : Windows.UI.Color.FromArgb(255, 246, 247, 249);
            return 1;
        }
        catch (Exception exception) { return Failure(exception); }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PixelSize { public uint Width, Height; }
    [StructLayout(LayoutKind.Sequential)]
    private struct BitmapProperties { public uint Format, Alpha; public float DpiX, DpiY; }

    private static unsafe void DrawPixels(CanvasDrawingSession session, nint pixels,
        int width, int height, int stride)
    {
        // Win2D's CanvasBitmap projection inherits XAML DependencyObject. Use
        // its supported native D2D interop instead, without a XAML application.
        Guid wrapperIid = new("5F10688D-EA55-4D55-A3B0-4DDB55C0C20A");
        Guid targetIid = new("2CD90694-12E2-11DC-9FED-001143A055F9");
        nint unknown = ((IWinRTObject)session).NativeObject.ThisPtr;
        nint wrapper = 0, target = 0;
        var query = (delegate* unmanaged[Stdcall]<nint, Guid*, nint*, int>)(*(nint**)unknown)[0];
        Marshal.ThrowExceptionForHR(query(unknown, &wrapperIid, &wrapper));
        try
        {
            var getNative = (delegate* unmanaged[Stdcall]<nint, nint, float, Guid*, nint*, int>)(*(nint**)wrapper)[3];
            Marshal.ThrowExceptionForHR(getNative(wrapper, 0, 96, &targetIid, &target));
            if (_uploadBitmap == 0)
            {
                PixelSize size = new() { Width = (uint)width, Height = (uint)height };
                BitmapProperties properties = new() { Format = 87, Alpha = 1, DpiX = 96, DpiY = 96 };
                nint bitmap = 0;
                // ID2D1RenderTarget::CreateBitmap, after IUnknown + GetFactory.
                var create = (delegate* unmanaged[Stdcall]<nint, PixelSize, nint, uint, BitmapProperties*, nint*, int>)(*(nint**)target)[4];
                Marshal.ThrowExceptionForHR(create(target, size, pixels, (uint)stride, &properties, &bitmap));
                _uploadBitmap = bitmap;
            }
            else
            {
                var copy = (delegate* unmanaged[Stdcall]<nint, nint, nint, uint, int>)(*(nint**)_uploadBitmap)[10];
                Marshal.ThrowExceptionForHR(copy(_uploadBitmap, 0, pixels, (uint)stride));
            }
            // ID2D1RenderTarget::DrawBitmap. Keep the drawing-session transform.
            var draw = (delegate* unmanaged[Stdcall]<nint, nint, nint, float, uint, nint, void>)(*(nint**)target)[26];
            draw(target, _uploadBitmap, 0, 1, 0, 0);
        }
        finally
        {
            ReleaseComPointer(target);
            ReleaseComPointer(wrapper);
        }
    }
}

public static class FrameExports
{
    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_shared_clip", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int SharedClip() => AcrylicBridge.HasSharedFrameClip;

    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_material", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Material(int dark, int enabled) => AcrylicBridge.SetFrameMaterial(dark != 0, enabled != 0);

    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_present", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Present(nint pixels, int width, int height, int stride)
        => AcrylicBridge.PresentFrame(pixels, width, height, stride);

    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_release", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Release() => AcrylicBridge.ReleaseFrames();

    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_pending", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Pending() => AcrylicBridge.PendingFrames;

    [UnmanagedCallersOnly(EntryPoint = "clipsave_frame_released", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Released() => AcrylicBridge.ReleasedFrames;
}
