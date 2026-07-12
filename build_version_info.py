from __future__ import annotations

import sys
import re
from pathlib import Path

from clipsave_app.constants import APP_NAME, APP_VERSION


def version_tuple(value: str) -> tuple[int, int, int, int]:
    numbers = []
    for part in value.split(".")[:4]:
        match = re.match(r"\d+", part)
        numbers.append(int(match.group(0)) if match else 0)
    return tuple((numbers + [0, 0, 0, 0])[:4])


def render() -> str:
    version = version_tuple(APP_VERSION)
    dotted = ".".join(str(value) for value in version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version},
    prodvers={version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{APP_NAME}'),
        StringStruct('FileDescription', '{APP_NAME} local clipboard library'),
        StringStruct('FileVersion', '{dotted}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{APP_VERSION}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)"""


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_version_info.py OUTPUT")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
