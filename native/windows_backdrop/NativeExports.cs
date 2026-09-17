using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;

namespace ClipSave.WindowsBackdrop;

public static class NativeExports
{
    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_is_supported", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int IsSupported() => AcrylicBridge.IsSupported();

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_attach", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Attach(nint hwnd, int dark) => AcrylicBridge.Attach(hwnd, dark != 0);

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_set_theme", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int SetTheme(int dark) => AcrylicBridge.SetTheme(dark != 0);

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_set_input_active", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int SetInputActive(int active) => AcrylicBridge.SetInputActive(active != 0);

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_detach", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int Detach() => AcrylicBridge.Detach();

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_last_error", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int LastError() => AcrylicBridge.LastError;

    [UnmanagedCallersOnly(EntryPoint = "clipsave_acrylic_last_stage", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int LastStage() => AcrylicBridge.LastStage;
}
