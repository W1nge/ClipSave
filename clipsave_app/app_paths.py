from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    base_dir: Path
    local_root: Path
    data_dir: Path
    library_dir: Path
    picture_dir: Path
    markdown_dir: Path
    legacy_data_dir: Path
    legacy_picture_dir: Path
    legacy_markdown_dir: Path
    usage_dir: Path
    database_path: Path
    settings_path: Path
    maintenance_dir: Path

    @classmethod
    def build(cls, *, base_dir: Path, local_root: Path) -> "AppPaths":
        base_dir = Path(base_dir)
        local_root = Path(local_root)
        data_dir = local_root / "Data"
        library_dir = local_root / "Library"
        return cls(
            base_dir=base_dir,
            local_root=local_root,
            data_dir=data_dir,
            library_dir=library_dir,
            picture_dir=library_dir / "Pictures",
            markdown_dir=library_dir / "Markdown",
            legacy_data_dir=base_dir / "data",
            legacy_picture_dir=base_dir / "Picture",
            legacy_markdown_dir=base_dir / "Markdown",
            usage_dir=data_dir / "Usage",
            database_path=data_dir / "clipsave.db",
            settings_path=data_dir / "settings.json",
            maintenance_dir=data_dir / "maintenance",
        )
