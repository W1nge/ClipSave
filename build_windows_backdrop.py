from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


TARGET_FRAMEWORK = "net8.0-windows10.0.17763.0"
BRIDGE_DLL = "clipsave_windows_backdrop.dll"

_PYINSTALLER_APPLICATION_SETTINGS = r"""
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly manifestVersion="1.0"
  xmlns:asmv3="urn:schemas-microsoft-com:asm.v3"
  xmlns="urn:schemas-microsoft-com:asm.v1">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"></requestedExecutionLevel>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{e2011457-1546-43c5-a5fe-008deee3d3f0}"></supportedOS>
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"></supportedOS>
      <supportedOS Id="{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}"></supportedOS>
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"></supportedOS>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"></supportedOS>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"></assemblyIdentity>
    </dependentAssembly>
  </dependency>
 </assembly>
"""


def _run_publish(project: Path) -> None:
    try:
        completed = subprocess.run(
            [
                "dotnet",
                "publish",
                str(project),
                "-c",
                "Release",
                "-r",
                "win-x64",
                "--self-contained",
                "true",
                "--nologo",
            ],
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "The .NET 8 SDK is required to build the Windows backdrop bridge."
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(f"dotnet publish failed with exit code {completed.returncode}")


def build(project_root: Path, output_root: Path) -> None:
    project = project_root / "native" / "windows_backdrop" / "WindowsBackdropBridge.csproj"
    if not project.is_file():
        raise RuntimeError(f"Missing backdrop project: {project}")

    native_root = project.parent
    # The bridge used to reference Windows App SDK and its build-generated
    # module initializer can survive an incremental publish after that package
    # is removed.  Release builds must therefore start from fresh native
    # intermediates or the resulting DLL may still try to load
    # Microsoft.WindowsAppRuntime.dll at process startup.
    for directory in (native_root / "bin", native_root / "obj"):
        if directory.exists():
            shutil.rmtree(directory)

    _run_publish(project)

    publish_dir = (
        native_root
        / "bin"
        / "Release"
        / TARGET_FRAMEWORK
        / "win-x64"
        / "publish"
    )
    runtime_dir = output_root / "runtime"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)

    bridge = publish_dir / BRIDGE_DLL
    if not bridge.is_file():
        raise RuntimeError(f"Windows backdrop publish is missing {bridge}")
    shutil.copy2(bridge, runtime_dir / BRIDGE_DLL)

    manifest_path = output_root / "ClipSave.manifest"
    manifest_path.write_text(
        _PYINSTALLER_APPLICATION_SETTINGS.strip() + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"Windows backdrop runtime: {runtime_dir}")
    print(f"Windows backdrop manifest: {manifest_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/windows_backdrop"),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent
    build(project_root, args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
