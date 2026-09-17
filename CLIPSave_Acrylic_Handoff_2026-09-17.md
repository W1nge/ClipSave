# ClipSave Acrylic Backend Handoff

## Final architecture

Win11: DWM System Acrylic.

Win10: Windows App SDK 1.8 DesktopAcrylicController + NativeAOT bridge + ctypes from PySide.

## Root cause

Old Win10 AccentPolicy Acrylic (ACCENT_ENABLE_ACRYLICBLURBEHIND=4) was rejected by benchmark:
- move ~13.77ms
- resize ~20.85ms

Modern Windows App SDK Acrylic:
- move 6.941ms mean in the final packaged ClipSave run
- resize 7.177ms mean in the final packaged ClipSave run

## Completed

- NativeAOT bridge integrated
- WinAppSDK runtime packaging
- manifest integration
- fallback logic
- build pipeline integration
- release smoke validation
- backend diagnostics
- full 528-test regression cleanup
- Windows cmd.exe/build.bat subprocess decoding cleanup
- packaged EXE move/resize performance verification

Verified release:
- `backdrop_backend=windows_app_sdk_acrylic`
- `backdrop_success=True`
- `backdrop_native_error=None`
- `event_loop_exited=0`

Final packaged EXE geometry run on Win10 19044 / 144.001 Hz:
- move: mean 6.941ms, p50 6.937ms, p95 7.147ms, p99 7.329ms, 0/240 >16.7ms
- resize: mean 7.177ms, p50 6.850ms, p95 9.155ms, p99 13.665ms, 1/240 >16.7ms, 0/240 >33.3ms

Final regression on the exact final working tree:
- `Ran 528 tests in 185.250s`
- `OK`
- no `_readerthread` / `UnicodeDecodeError` noise

## Resolved test-runner issue

The apparent unittest hangs were Windows subprocess decode failures: this Python runs with UTF-8 mode enabled while `cmd.exe` emits the local Windows code page. The affected tests now use `locale.getencoding()` plus `errors="replace"` for captured `cmd.exe` / `build.bat` text. On this machine that resolves to `cp936`. Do not switch those subprocess captures back to implicit UTF-8 decoding.

## Remaining

- no Acrylic/backend blocker is known
- keep the normal release/build validation when future production code changes

## Do not revisit

Do not return to AccentPolicy Acrylic tuning. It was benchmarked and is not the correct path.
