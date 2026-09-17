from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


WINDOWS_APP_SDK_VERSION = "1.8.260804001"
TARGET_FRAMEWORK = "net8.0-windows10.0.17763.0"
RUNTIME_SUBDIR = r"_internal\windows_backdrop"

RUNTIME_FILES = (
    "clipsave_windows_backdrop.dll",
    "CoreMessagingXP.dll",
    "dcompi.dll",
    "dwmcorei.dll",
    "DwmSceneI.dll",
    "marshal.dll",
    "Microsoft.InputStateManager.dll",
    "Microsoft.Internal.FrameworkUdk.dll",
    "Microsoft.UI.Composition.OSSupport.dll",
    "Microsoft.UI.dll",
    "Microsoft.UI.Input.dll",
    "Microsoft.UI.Windowing.Core.dll",
    "Microsoft.UI.Windowing.dll",
    "Microsoft.WindowsAppRuntime.dll",
    "Microsoft.UI.pri",
    "Microsoft.WindowsAppRuntime.pri",
    "wuceffectsi.dll",
)

_FILE_BLOCK = re.compile(
    r"(?P<indent>\s*)<asmv3:file name=\"(?P<name>[^\"]+)\">.*?</asmv3:file>\s*",
    re.DOTALL,
)

_PYINSTALLER_APPLICATION_SETTINGS = r"""
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


def _filtered_manifest(source: Path) -> str:
    text = source.read_text(encoding="utf-8-sig")
    keep = set(RUNTIME_FILES)
    kept_blocks: list[str] = []
    for match in _FILE_BLOCK.finditer(text):
        name = match.group("name")
        if name not in keep:
            continue
        block = match.group(0).strip()
        block = block.replace(
            f'<asmv3:file name="{name}">',
            f'<asmv3:file name="{RUNTIME_SUBDIR}\\{name}">',
            1,
        )
        kept_blocks.append("    " + block.lstrip())

    required_manifest_files = {
        "CoreMessagingXP.dll",
        "dcompi.dll",
        "Microsoft.UI.Input.dll",
        "Microsoft.UI.Windowing.dll",
        "Microsoft.UI.Windowing.Core.dll",
        "Microsoft.WindowsAppRuntime.dll",
        "wuceffectsi.dll",
    }
    found = {
        match.group("name")
        for match in _FILE_BLOCK.finditer(text)
        if match.group("name") in keep
    }
    missing = sorted(required_manifest_files - found)
    if missing:
        raise RuntimeError(
            "Windows App SDK manifest is missing required registrations: "
            + ", ".join(missing)
        )

    header = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly manifestVersion="1.0" '
        'xmlns:asmv3="urn:schemas-microsoft-com:asm.v3" '
        'xmlns:winrtv1="urn:schemas-microsoft-com:winrt.v1" '
        'xmlns="urn:schemas-microsoft-com:asm.v1">\n'
    )
    body = "\n".join(kept_blocks)
    return header + body + "\n" + _PYINSTALLER_APPLICATION_SETTINGS + "</assembly>\n"


def build(project_root: Path, output_root: Path) -> None:
    project = project_root / "native" / "windows_backdrop" / "WindowsBackdropBridge.csproj"
    if not project.is_file():
        raise RuntimeError(f"Missing backdrop project: {project}")

    _run_publish(project)

    native_root = project.parent
    publish_dir = (
        native_root
        / "bin"
        / "Release"
        / TARGET_FRAMEWORK
        / "win-x64"
        / "publish"
    )
    manifest_source = (
        native_root
        / "obj"
        / "Release"
        / TARGET_FRAMEWORK
        / "win-x64"
        / "Manifests"
        / "app.manifest"
    )
    if not manifest_source.is_file():
        raise RuntimeError(f"Windows App SDK did not generate {manifest_source}")

    runtime_dir = output_root / "runtime"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)

    missing_runtime = [name for name in RUNTIME_FILES if not (publish_dir / name).is_file()]
    if missing_runtime:
        raise RuntimeError(
            "Windows backdrop publish is missing required runtime files: "
            + ", ".join(missing_runtime)
        )
    for name in RUNTIME_FILES:
        shutil.copy2(publish_dir / name, runtime_dir / name)

    manifest_path = output_root / "ClipSave.manifest"
    manifest_path.write_text(_filtered_manifest(manifest_source), encoding="utf-8", newline="\n")

    nuget_license = (
        Path.home()
        / ".nuget"
        / "packages"
        / "microsoft.windowsappsdk"
        / WINDOWS_APP_SDK_VERSION
        / "license.txt"
    )
    if not nuget_license.is_file():
        raise RuntimeError(f"Windows App SDK license file is missing: {nuget_license}")
    shutil.copy2(nuget_license, output_root / "WindowsAppSDK-LICENSE.txt")

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
