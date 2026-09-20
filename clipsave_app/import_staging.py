from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from PIL import Image

from . import storage


@dataclass(slots=True)
class StagedImport:
    kind: str
    path: Path
    content: str
    mime: str
    content_hash: str
    created_at: dt.datetime
    file_size: int
    width: int | None
    height: int | None
    source: str
    external: int
    managed_local: bool
    identity_handle: BinaryIO
    created_copy_path: Path | None = None
    created_copy_root: Path | None = None
    created_copy_hash: str | None = None
    created_copy_size: int | None = None

    @property
    def created_copy(self) -> bool:
        return self.created_copy_path is not None

    def close(self) -> None:
        handle = self.identity_handle
        self.identity_handle = None  # type: ignore[assignment]
        if handle is not None:
            handle.close()

    def discard_created_copy(self) -> None:
        path = self.created_copy_path
        root = self.created_copy_root
        if path is None or root is None:
            return
        self.close()
        try:
            if path.exists():
                storage.delete_managed_file(
                    path,
                    root,
                    expected_sha256=self.created_copy_hash,
                    expected_size=self.created_copy_size,
                )
        except (OSError, RuntimeError):
            pass
        finally:
            self.created_copy_path = None
            self.created_copy_root = None
            self.created_copy_hash = None
            self.created_copy_size = None


def prepare_import(
    path: Path,
    kind: str | None,
    copy_to_library: bool,
    *,
    strict: bool,
    picture_dir: Path,
    markdown_dir: Path,
    is_local: Callable[[Path], bool],
    stream_hash: Callable[[BinaryIO], str],
    max_import_bytes: int,
    max_markdown_bytes: int,
    max_image_pixels: int,
) -> StagedImport | None:
    created_path: Path | None = None
    created_root: Path | None = None
    created_hash: str | None = None
    created_size: int | None = None
    identity_handle: BinaryIO | None = None
    try:
        path = storage.normalized_absolute_path(path.resolve())
        if not path.exists() or not path.is_file():
            if strict:
                raise FileNotFoundError(f"Import file does not exist: {path}")
            return None

        kind = kind or ("markdown" if path.suffix.lower() == ".md" else "image")
        if kind not in {"image", "markdown"}:
            if strict:
                raise ValueError(f"Unsupported import kind: {kind}")
            return None

        source_root = (
            markdown_dir if kind == "markdown" else picture_dir
        ) if is_local(path) else path.parent
        with storage.open_managed_binary(
            path,
            "rb",
            source_root,
            identity_locked=True,
        ) as source_snapshot:
            source_stat = os.fstat(source_snapshot.fileno())
            if source_stat.st_size > max_import_bytes or (
                kind == "markdown" and source_stat.st_size > max_markdown_bytes
            ):
                if strict:
                    raise ValueError("Import file exceeds the configured size limit")
                return None
            source_hash = stream_hash(source_snapshot)

        if copy_to_library and not is_local(path):
            managed_root = markdown_dir if kind == "markdown" else picture_dir
            target_dir = managed_root / "Imported"
            storage.validate_managed_write_path(target_dir / "import.tmp", managed_root)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            stem, suffix = path.stem, path.suffix
            index = 2
            while True:
                try:
                    with storage.open_managed_binary(target, "rb", managed_root) as existing:
                        if stream_hash(existing) == source_hash:
                            break
                except FileNotFoundError:
                    temporary = target_dir / f".import-{secrets.token_hex(16)}{suffix}.tmp"
                    created_path = temporary
                    created_root = managed_root
                    try:
                        with storage.open_managed_binary(
                            path,
                            "rb",
                            source_root,
                            identity_locked=True,
                        ) as source, storage.open_managed_binary(
                            temporary,
                            "xb",
                            managed_root,
                        ) as destination:
                            digest = hashlib.sha256()
                            created_size = 0
                            while chunk := source.read(1024 * 1024):
                                destination.write(chunk)
                                digest.update(chunk)
                                created_size += len(chunk)
                        created_hash = digest.hexdigest()
                        if (
                            created_hash != source_hash
                            or created_size != source_stat.st_size
                        ):
                            _discard_copy(
                                created_path,
                                created_root,
                                created_hash,
                                created_size,
                            )
                            if strict:
                                raise RuntimeError(
                                    "Imported file changed while it was being copied"
                                )
                            return None
                        storage.validate_managed_write_path(target, managed_root)
                        if os.name == "nt":
                            os.rename(temporary, target)
                        else:
                            os.link(temporary, target)
                            os.unlink(temporary)
                        created_path = target
                        break
                    except FileExistsError:
                        _discard_copy(
                            created_path,
                            created_root,
                            created_hash,
                            created_size,
                        )
                        created_path = None
                        created_root = None
                        created_hash = None
                        created_size = None
                        continue
                target = target_dir / f"{stem} ({index}){suffix}"
                index += 1
            path = storage.normalized_absolute_path(target)

        identity_root = (
            markdown_dir if kind == "markdown" else picture_dir
        ) if is_local(path) else path.parent
        identity_handle = storage.open_managed_binary(
            path,
            "rb",
            identity_root,
            identity_locked=True,
        )
        final_stat = os.fstat(identity_handle.fileno())
        identity_handle.seek(0)
        final_hash = stream_hash(identity_handle)
        if final_hash != source_hash or final_stat.st_size != source_stat.st_size:
            raise ValueError("Imported file changed before it could be indexed")

        width = height = None
        content = ""
        if kind == "image":
            try:
                identity_handle.seek(0)
                with Image.open(identity_handle) as image:
                    width, height = image.size
                    if width * height > max_image_pixels:
                        raise ValueError("Image file exceeds the configured pixel limit")
                    image.load()
            except (OSError, ValueError):
                raise ValueError(f"Image file is invalid or unreadable: {path}")
        else:
            identity_handle.seek(0)
            content = identity_handle.read().decode("utf-8", errors="replace")

        managed_local = is_local(path)
        return StagedImport(
            kind=kind,
            path=path,
            content=content,
            mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            content_hash=source_hash,
            created_at=dt.datetime.fromtimestamp(final_stat.st_mtime),
            file_size=final_stat.st_size,
            width=width,
            height=height,
            source="导入文件" if copy_to_library else "现有文件",
            external=int(not managed_local),
            managed_local=managed_local,
            identity_handle=identity_handle,
            created_copy_path=created_path,
            created_copy_root=created_root,
            created_copy_hash=created_hash,
            created_copy_size=created_size,
        )
    except BaseException:
        if identity_handle is not None:
            identity_handle.close()
        _discard_copy(created_path, created_root, created_hash, created_size)
        raise


def _discard_copy(
    path: Path | None,
    root: Path | None,
    digest: str | None,
    size: int | None,
) -> None:
    if path is None or root is None:
        return
    try:
        if path.exists():
            storage.delete_managed_file(
                path,
                root,
                expected_sha256=digest,
                expected_size=size,
            )
    except (OSError, RuntimeError):
        pass
