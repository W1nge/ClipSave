from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def build_manifest(root: Path, output: Path) -> int:
    root = root.resolve()
    output = output.resolve()
    lines: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda value: str(value).lower()):
        if not path.is_file() or path.resolve() == output:
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"Release file escaped release root: {path}") from exc
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest().upper()}  {relative}")
    if not lines:
        raise RuntimeError("Release checksum manifest is empty")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return len(lines)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_manifest.py RELEASE_ROOT OUTPUT")
    build_manifest(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
