using System.Runtime.InteropServices;

namespace ClipSave.WindowsBackdrop;

internal static class CoreMessagingHelper
{
    private enum DispatcherQueueThreadApartmentType
    {
        None = 0,
        Asta = 1,
        Sta = 2,
    }

    private enum DispatcherQueueThreadType
    {
        Dedicated = 1,
        Current = 2,
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DispatcherQueueOptions
    {
        public int Size;
        public DispatcherQueueThreadType ThreadType;
        public DispatcherQueueThreadApartmentType ApartmentType;
    }

    [DllImport("CoreMessaging.dll", ExactSpelling = true)]
    private static extern uint CreateDispatcherQueueController(
        DispatcherQueueOptions options,
        out IntPtr dispatcherQueueController);

    internal static Windows.System.DispatcherQueueController CreateForCurrentThread()
    {
        var options = new DispatcherQueueOptions
        {
            Size = Marshal.SizeOf<DispatcherQueueOptions>(),
            ThreadType = DispatcherQueueThreadType.Current,
            ApartmentType = DispatcherQueueThreadApartmentType.None,
        };
        uint hr = CreateDispatcherQueueController(options, out IntPtr pointer);
        if (hr != 0)
        {
            Marshal.ThrowExceptionForHR(unchecked((int)hr));
        }

        try
        {
            return Windows.System.DispatcherQueueController.FromAbi(pointer);
        }
        finally
        {
            Marshal.Release(pointer);
        }
    }
}
