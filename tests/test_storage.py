import ctypes
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from ctypes import wintypes
from unittest.mock import Mock, patch

from clipsave_app import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_rejects_same_source_and_target(self):
        source = self.root / "same"
        source.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")

        moved = storage._copy_or_move_contents(source, source)

        self.assertEqual(moved, 0)
        self.assertTrue(item.exists())

    def test_migration_rejects_source_or_target_reparse_points(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")

        for rejected in (source, target):
            with self.subTest(rejected=rejected):
                with patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == rejected):
                    self.assertEqual(storage._copy_or_move_contents(source, target), 0)
                self.assertTrue(item.exists())

    def test_migration_skips_existing_reparse_destination(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")
        destination = target / item.name

        with patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == destination):
            moved = storage._copy_or_move_contents(source, target)

        self.assertEqual(moved, 0)
        self.assertTrue(item.exists())

    def test_migration_retry_reuses_identical_cross_volume_copy(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        item = source / "resume.txt"
        destination = target / item.name
        item.write_text("copied before source cleanup failed", encoding="utf-8")
        shutil.copy2(item, destination)

        moved = storage._copy_or_move_contents(source, target)

        self.assertEqual(moved, 1)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "copied before source cleanup failed")
        self.assertEqual(list(target.glob("resume*")), [destination])

    def test_first_migration_copy_verifies_before_deleting_without_shutil_move(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        item = source / "first.txt"
        item.write_text("first protected copy", encoding="utf-8")

        with patch.object(storage.shutil, "move") as unsafe_move:
            moved = storage._copy_or_move_contents(source, target)

        unsafe_move.assert_not_called()
        self.assertEqual(moved, 1)
        self.assertFalse(item.exists())
        self.assertEqual((target / item.name).read_text(encoding="utf-8"), "first protected copy")

    def test_first_migration_keeps_source_when_verified_delete_fails(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        item = source / "keep.txt"
        item.write_text("keep both", encoding="utf-8")

        with patch.object(storage, "_delete_source_if_identical", return_value=False):
            moved = storage._copy_or_move_contents(source, target)

        self.assertEqual(moved, 0)
        self.assertEqual(item.read_text(encoding="utf-8"), "keep both")
        self.assertEqual((target / item.name).read_text(encoding="utf-8"), "keep both")

    def test_legacy_history_migration_uses_protected_copy(self):
        base = self.root / "legacy-base"
        data = self.root / "data"
        library = self.root / "library"
        base.mkdir()
        history = base / "clipsave_history.json"
        history.write_text('{"history": true}', encoding="utf-8")

        with (
            patch.multiple(
                storage,
                BASE_DIR=base,
                LEGACY_DATA_DIR=base / "data",
                LEGACY_PICTURE_DIR=base / "pictures",
                LEGACY_MARKDOWN_DIR=base / "markdown",
                DATA_DIR=data,
                LIBRARY_DIR=library,
                PICTURE_DIR=library / "Pictures",
                MARKDOWN_DIR=library / "Markdown",
            ),
            patch.object(storage.shutil, "move") as unsafe_move,
        ):
            result = storage.migrate_legacy_layout()

        unsafe_move.assert_not_called()
        self.assertEqual(result["data"], 1)
        self.assertFalse(history.exists())
        self.assertEqual(
            (data / history.name).read_text(encoding="utf-8"), '{"history": true}'
        )

    @unittest.skipUnless(os.name == "nt", "verified deletion locking is Windows-only")
    def test_migration_retry_locks_source_until_identical_copy_is_confirmed(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        item = source / "locked.txt"
        destination = target / item.name
        item.write_bytes(b"old-state")
        shutil.copy2(item, destination)
        real_mark = storage._mark_handle_for_delete
        mutation_blocked = []

        def verify_source_is_locked(handle):
            with self.assertRaises(OSError):
                item.write_bytes(b"new-state")
            mutation_blocked.append(True)
            real_mark(handle)

        with patch.object(
            storage, "_mark_handle_for_delete", side_effect=verify_source_is_locked
        ):
            self.assertEqual(storage._copy_or_move_contents(source, target), 1)

        self.assertEqual(mutation_blocked, [True])
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"old-state")

    def test_local_store_requires_lexical_and_resolved_containment(self):
        library = self.root / "Library"
        library.mkdir()
        inside = library / "Pictures" / "future.png"
        outside = self.root / "Library-other" / "outside.png"

        with patch.object(storage, "LIBRARY_DIR", library):
            self.assertTrue(storage.is_under_local_store(inside))
            self.assertFalse(storage.is_under_local_store(outside))

    def test_local_store_rejects_symlink_escape(self):
        library = self.root / "Library"
        outside = self.root / "outside"
        library.mkdir()
        outside.mkdir()
        link = library / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("directory symlinks are unavailable")

        with patch.object(storage, "LIBRARY_DIR", library):
            self.assertFalse(storage.is_under_local_store(link / "item.png"))

    def test_managed_write_rejects_existing_leaf_link(self):
        library = self.root / "Library"
        outside = self.root / "outside.txt"
        library.mkdir()
        outside.write_text("outside", encoding="utf-8")
        linked = library / "linked.txt"
        try:
            os.symlink(outside, linked)
        except (NotImplementedError, OSError):
            self.skipTest("file symlinks are unavailable")

        with self.assertRaisesRegex(RuntimeError, "reparse point"):
            storage.validate_managed_write_path(linked, library)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_managed_write_rejects_existing_hard_link(self):
        library = self.root / "Library"
        outside = self.root / "outside.txt"
        library.mkdir()
        outside.write_text("outside", encoding="utf-8")
        linked = library / "linked.txt"
        try:
            os.link(outside, linked)
        except OSError:
            self.skipTest("hard links are unavailable")

        with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
            storage.validate_managed_write_path(linked, library)

    def test_managed_binary_round_trip_and_permanent_delete(self):
        library = self.root / "Library"
        library.mkdir()
        target = library / "managed.bin"

        with storage.open_managed_binary(target, "xb", library) as handle:
            handle.write(b"managed payload")
        with storage.open_managed_binary(target, "rb", library) as handle:
            self.assertEqual(handle.read(), b"managed payload")

        storage.delete_managed_file(target, library)
        self.assertFalse(target.exists())

    def test_recycle_uses_original_filename_and_deletes_verified_original(self):
        library = self.root / "Library"
        library.mkdir()
        target = library / "managed.bin"
        target.write_bytes(b"payload")
        recycled = []

        def recycler(value: str) -> None:
            staged = Path(value)
            recycled.append((staged.name, staged.parent.parent, staged.read_bytes()))
            staged.unlink()

        storage.recycle_managed_file(target, library, recycler)

        self.assertEqual(recycled, [(target.name, library, b"payload")])
        self.assertFalse(target.exists())
        self.assertEqual(list(library.iterdir()), [])

    def test_recycle_failure_keeps_original_and_cleans_staging_directory(self):
        library = self.root / "Library"
        library.mkdir()
        target = library / "managed.bin"
        target.write_bytes(b"payload")

        with self.assertRaisesRegex(OSError, "recycle failed"):
            storage.recycle_managed_file(
                target,
                library,
                lambda _value: (_ for _ in ()).throw(OSError("recycle failed")),
            )

        self.assertEqual(target.read_bytes(), b"payload")
        self.assertEqual(list(library.iterdir()), [target])

    def test_recycle_rejects_changed_content_before_recycler(self):
        library = self.root / "Library"
        library.mkdir()
        target = library / "managed.bin"
        target.write_bytes(b"replacement")
        recycler = Mock()

        with self.assertRaisesRegex(RuntimeError, "content changed"):
            storage.recycle_managed_file(
                target,
                library,
                recycler,
                expected_sha256="0" * 64,
                expected_size=len(b"replacement"),
            )

        recycler.assert_not_called()
        self.assertEqual(target.read_bytes(), b"replacement")

    @unittest.skipUnless(os.name == "nt", "Windows handle deletion")
    def test_recycle_does_not_delete_path_replacement(self):
        library = self.root / "Library"
        library.mkdir()
        target = library / "managed.bin"
        moved_original = library / "moved-original.bin"
        target.write_bytes(b"original")

        def recycler(value: str) -> None:
            Path(value).unlink()
            os.replace(target, moved_original)
            target.write_bytes(b"replacement")

        storage.recycle_managed_file(target, library, recycler)

        self.assertEqual(target.read_bytes(), b"replacement")
        self.assertFalse(moved_original.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle validation")
    def test_managed_directory_accepts_equivalent_short_path(self):
        get_short_path = storage._kernel32_function(
            "GetShortPathNameW",
            [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD],
            wintypes.DWORD,
        )
        required = get_short_path(str(self.root), None, 0)
        if not required:
            self.skipTest("8.3 short paths are unavailable")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_short_path(str(self.root), buffer, len(buffer))
        if not written or written >= len(buffer) or buffer.value == str(self.root):
            self.skipTest("test directory has no distinct 8.3 short path")

        with storage.hold_managed_directory(Path(buffer.value)) as held:
            self.assertEqual(
                storage._normalized_requested_path(held),
                storage._normalized_requested_path(self.root),
            )

    @unittest.skipUnless(os.name == "nt", "Windows handle validation")
    def test_handle_open_rejects_existing_symlink_before_payload_write(self):
        library = self.root / "Library"
        outside = self.root / "outside.bin"
        library.mkdir()
        outside.write_bytes(b"outside")
        linked = library / "linked.bin"
        try:
            os.symlink(outside, linked)
        except OSError:
            self.skipTest("file symlinks are unavailable")

        with self.assertRaisesRegex(RuntimeError, "reparse point"):
            storage.open_managed_binary(linked, "wb", library)
        self.assertEqual(outside.read_bytes(), b"outside")

    @unittest.skipUnless(os.name == "nt", "Windows handle validation")
    def test_handle_open_and_delete_reject_existing_hardlink(self):
        library = self.root / "Library"
        outside = self.root / "outside.bin"
        library.mkdir()
        outside.write_bytes(b"outside")
        linked = library / "linked.bin"
        os.link(outside, linked)

        with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
            storage.open_managed_binary(linked, "wb", library)
        with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
            storage.delete_managed_file(linked, library)
        self.assertEqual(outside.read_bytes(), b"outside")

    @unittest.skipUnless(os.name == "nt", "Windows Junction behavior")
    def test_handle_open_catches_parent_junction_swap(self):
        library = self.root / "Library"
        parent = library / "parent"
        outside = self.root / "outside"
        parent.mkdir(parents=True)
        outside.mkdir()
        target = parent / "blocked.bin"
        real_create_file = storage._create_file
        swapped = False

        def swap_parent_before_leaf_open(
            path, desired_access, creation_disposition, flags=None, share_mode=None
        ):
            nonlocal swapped
            if (
                storage.normalized_absolute_path(Path(path))
                == storage.normalized_absolute_path(target)
                and not swapped
            ):
                swapped = True
                parent.rmdir()
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(parent), str(outside)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    self.skipTest(f"could not create Junction: {result.stderr or result.stdout}")
            kwargs = {}
            if flags is not None:
                kwargs["flags"] = flags
            if share_mode is not None:
                kwargs["share_mode"] = share_mode
            return real_create_file(path, desired_access, creation_disposition, **kwargs)

        try:
            with patch.object(storage, "_create_file", side_effect=swap_parent_before_leaf_open):
                with self.assertRaisesRegex(RuntimeError, "escaped its local root"):
                    storage.open_managed_binary(target, "xb", library)
            self.assertFalse((outside / target.name).exists())
        finally:
            if parent.is_junction():
                parent.rmdir()

    def test_storage_layout_rejects_unc_root(self):
        unc_root = Path(r"\\server\share\ClipSave")
        with patch.multiple(
            storage,
            LOCAL_ROOT=unc_root,
            DATA_DIR=unc_root / "Data",
            LIBRARY_DIR=unc_root / "Library",
            PICTURE_DIR=unc_root / "Library" / "Pictures",
            MARKDOWN_DIR=unc_root / "Library" / "Markdown",
            THUMB_DIR=unc_root / "Data" / "thumbnails",
            MAINTENANCE_DIR=unc_root / "Data" / "maintenance",
        ):
            with self.assertRaisesRegex(RuntimeError, "network path"):
                storage.validate_storage_layout()

    def test_storage_layout_rejects_reparse_root(self):
        local_root = self.root / "ClipSave"
        paths = {
            "LOCAL_ROOT": local_root,
            "DATA_DIR": local_root / "Data",
            "LIBRARY_DIR": local_root / "Library",
            "PICTURE_DIR": local_root / "Library" / "Pictures",
            "MARKDOWN_DIR": local_root / "Library" / "Markdown",
            "THUMB_DIR": local_root / "Data" / "thumbnails",
            "MAINTENANCE_DIR": local_root / "Data" / "maintenance",
        }
        with (
            patch.multiple(storage, **paths),
            patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == local_root),
        ):
            with self.assertRaisesRegex(RuntimeError, "Junction"):
                storage.validate_storage_layout()

    def test_storage_directories_are_created_only_after_validation(self):
        local_root = self.root / "ClipSave"
        paths = {
            "LOCAL_ROOT": local_root,
            "DATA_DIR": local_root / "Data",
            "LIBRARY_DIR": local_root / "Library",
            "PICTURE_DIR": local_root / "Library" / "Pictures",
            "MARKDOWN_DIR": local_root / "Library" / "Markdown",
            "THUMB_DIR": local_root / "Data" / "thumbnails",
            "MAINTENANCE_DIR": local_root / "Data" / "maintenance",
        }
        with patch.multiple(storage, **paths):
            storage.ensure_storage_directories()
        for path in paths.values():
            self.assertTrue(path.is_dir())

    def test_safe_file_iteration_prunes_reparse_directories(self):
        library = self.root / "Library"
        normal = library / "normal"
        linked = library / "linked"
        normal.mkdir(parents=True)
        linked.mkdir()
        keep = normal / "keep.png"
        outside = linked / "outside.png"
        keep.write_bytes(b"keep")
        outside.write_bytes(b"outside")

        with patch.object(storage, "_is_link_or_junction", side_effect=lambda path: Path(path) == linked):
            found = list(storage.iter_safe_files(library, (".png",)))

        self.assertEqual(found, [keep])

    @unittest.skipUnless(os.name == "nt", "Windows Junction behavior")
    def test_managed_write_rejects_live_junction_ancestor(self):
        library = self.root / "Library"
        outside = self.root / "outside"
        library.mkdir()
        outside.mkdir()
        junction = library / "linked"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"could not create Junction: {result.stderr or result.stdout}")
        try:
            with self.assertRaisesRegex(RuntimeError, "managed local library|reparse point"):
                storage.validate_managed_write_path(junction / "blocked.png", library)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            junction.rmdir()

    def test_legacy_database_collision_stops_without_moving_either_database(self):
        legacy_data = self.root / "legacy-data"
        data = self.root / "data"
        library = self.root / "library"
        legacy_data.mkdir()
        data.mkdir()
        legacy = legacy_data / "clipsave.db"
        active = data / "clipsave.db"
        legacy.write_bytes(b"legacy")
        active.write_bytes(b"active")

        with patch.multiple(
            storage,
            LEGACY_DATA_DIR=legacy_data,
            DATA_DIR=data,
            LIBRARY_DIR=library,
            PICTURE_DIR=library / "Pictures",
            MARKDOWN_DIR=library / "Markdown",
        ):
            with self.assertRaisesRegex(RuntimeError, "Both the legacy and current"):
                storage.migrate_legacy_layout()

        self.assertEqual(legacy.read_bytes(), b"legacy")
        self.assertEqual(active.read_bytes(), b"active")

    def test_identical_legacy_database_collision_resumes_migration(self):
        legacy_data = self.root / "legacy-data"
        data = self.root / "data"
        library = self.root / "library"
        legacy_data.mkdir()
        data.mkdir()
        legacy = legacy_data / "clipsave.db"
        active = data / "clipsave.db"
        connection = sqlite3.connect(active)
        connection.execute("CREATE TABLE items(value TEXT)")
        connection.execute("INSERT INTO items VALUES('identical')")
        connection.commit()
        connection.close()
        shutil.copy2(active, legacy)

        with patch.multiple(
            storage,
            LEGACY_DATA_DIR=legacy_data,
            DATA_DIR=data,
            LIBRARY_DIR=library,
            PICTURE_DIR=library / "Pictures",
            MARKDOWN_DIR=library / "Markdown",
        ):
            storage.migrate_legacy_layout()

        connection = sqlite3.connect(active)
        try:
            self.assertEqual(
                connection.execute("SELECT value FROM items").fetchone()[0],
                "identical",
            )
        finally:
            connection.close()
        preserved = list(data.glob("clipsave.db.migrated-duplicate*"))
        self.assertEqual(len(preserved), 1)
        connection = sqlite3.connect(preserved[0])
        try:
            self.assertEqual(
                connection.execute("SELECT value FROM items").fetchone()[0],
                "identical",
            )
        finally:
            connection.close()

    def test_divergent_wal_databases_are_not_treated_as_identical(self):
        legacy_data = self.root / "legacy-data"
        data = self.root / "data"
        library = self.root / "library"
        legacy_data.mkdir()
        data.mkdir()
        legacy = legacy_data / "clipsave.db"
        active = data / "clipsave.db"
        base = sqlite3.connect(active)
        base.execute("CREATE TABLE items(value TEXT)")
        base.execute("INSERT INTO items VALUES('base')")
        base.commit()
        base.close()
        shutil.copy2(active, legacy)

        active_connection = sqlite3.connect(active)
        legacy_connection = sqlite3.connect(legacy)
        for connection, value in (
            (active_connection, "active-only"),
            (legacy_connection, "legacy-only"),
        ):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO items VALUES(?)", (value,))
            connection.commit()
        try:
            with patch.multiple(
                storage,
                LEGACY_DATA_DIR=legacy_data,
                DATA_DIR=data,
                LIBRARY_DIR=library,
                PICTURE_DIR=library / "Pictures",
                MARKDOWN_DIR=library / "Markdown",
            ):
                with self.assertRaisesRegex(RuntimeError, "Both the legacy and current"):
                    storage.migrate_legacy_layout()
        finally:
            active_connection.close()
            legacy_connection.close()

        self.assertTrue(active.exists())
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
