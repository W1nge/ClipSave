from __future__ import annotations

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement


LICENSE_PATTERN = re.compile(r"^(license|licence|copying|notice)", re.IGNORECASE)


def requirement_names(*paths: Path) -> list[str]:
    names: set[str] = set()
    for path in paths:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                names.add(Requirement(line).name)
    return sorted(names, key=str.lower)


def collect(output: Path) -> None:
    required_upstream = {
        "CPython-PSF-2.0.txt",
        "OpenSSL-Apache-2.0.txt",
        "Qt-LGPL-3.0-only.txt",
        "Qt-GPL-3.0-only.txt",
        "SQLite-Public-Domain.txt",
        "pywinrt-MIT.txt",
    }
    upstream_dir = Path("third_party_licenses")
    missing_upstream = [
        name
        for name in sorted(required_upstream)
        if not (upstream_dir / name).is_file() or not (upstream_dir / name).stat().st_size
    ]
    if missing_upstream:
        raise RuntimeError("Missing required upstream license text: " + ", ".join(missing_upstream))
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(upstream_dir, output / "UPSTREAM_LICENSES")
    missing: list[str] = []
    for name in requirement_names(Path("requirements.txt"), Path("build-requirements.txt")):
        distribution = metadata.distribution(name)
        destination = output / re.sub(r"[^A-Za-z0-9._-]+", "_", f"{name}-{distribution.version}")
        copied = 0
        for entry in distribution.files or ():
            source = Path(distribution.locate_file(entry))
            if not source.is_file() or not LICENSE_PATTERN.search(source.name):
                continue
            relative = Path(*entry.parts[-3:])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        metadata_text = distribution.read_text("METADATA")
        if metadata_text:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "METADATA").write_text(metadata_text, encoding="utf-8")
        if copied == 0:
            missing.append(name)
    uncovered = [
        name
        for name in missing
        if name not in {"PySide6-Essentials", "shiboken6", "winrt-runtime"}
        and not name.startswith("winrt-Windows.")
    ]
    if uncovered:
        raise RuntimeError("No wheel or upstream license text found for: " + ", ".join(uncovered))


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: collect_third_party_licenses.py OUTPUT_DIR")
    collect(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
