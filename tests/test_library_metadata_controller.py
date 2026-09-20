import unittest

from clipsave_app.library_metadata_controller import LibraryMetadataController


class FakeDatabase:
    def __init__(self):
        self.calls = []
        self.failure = None

    def _call(self, name, *args):
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure
        return 17

    def set_favorite(self, *args): return self._call("set_favorite", *args)
    def set_notes(self, *args): return self._call("set_notes", *args)
    def create_collection(self, *args): return self._call("create_collection", *args)
    def delete_collection(self, *args): return self._call("delete_collection", *args)
    def delete_tag(self, *args): return self._call("delete_tag", *args)
    def add_tag(self, *args): return self._call("add_tag", *args)
    def remove_tag(self, *args): return self._call("remove_tag", *args)
    def set_collection(self, *args): return self._call("set_collection", *args)


class LibraryMetadataControllerTests(unittest.TestCase):
    def test_explicit_mutations_delegate_to_database(self):
        database = FakeDatabase()
        controller = LibraryMetadataController(database)

        result = controller.set_collection(7, 3)
        controller.add_tag(7, "Work")
        controller.set_favorite(7, True)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.value, 17)
        self.assertEqual(
            database.calls,
            [
                ("set_collection", (7, 3)),
                ("add_tag", (7, "Work")),
                ("set_favorite", (7, True)),
            ],
        )

    def test_database_failure_is_returned_without_becoming_ui_policy(self):
        database = FakeDatabase()
        database.failure = RuntimeError("blocked")
        controller = LibraryMetadataController(database)

        result = controller.delete_tag(4)

        self.assertFalse(result.succeeded)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(str(result.error), "blocked")
