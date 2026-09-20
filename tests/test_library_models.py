import unittest

from clipsave_app.library_models import (
    CollectionSummary,
    LibraryItem,
    LibraryQuery,
    LibraryViewState,
    TagSummary,
)


class LibraryQueryTests(unittest.TestCase):
    def test_database_kwargs_preserve_query_contract(self):
        query = LibraryQuery(
            query="needle",
            query_terms=("needle", "pin"),
            kind="image",
            favorite=True,
            day="2026-09-20",
            recent_days=7,
            collection_id=3,
            tag_id=4,
            sort="oldest",
        )

        self.assertEqual(
            query.as_database_kwargs(),
            {
                "query": "needle",
                "query_terms": ("needle", "pin"),
                "kind": "image",
                "favorite": True,
                "day": "2026-09-20",
                "recent_days": 7,
                "collection_id": 3,
                "tag_id": 4,
                "sort": "oldest",
            },
        )

    def test_query_is_hashable_and_compares_by_value(self):
        self.assertEqual(LibraryQuery(query="x"), LibraryQuery(query="x"))
        self.assertEqual(len({LibraryQuery(query="x"), LibraryQuery(query="x")}), 1)


class LibraryViewStateTests(unittest.TestCase):
    def test_view_state_owns_filter_selection_and_pagination_defaults(self):
        state = LibraryViewState(sort="oldest")

        self.assertEqual(state.items, [])
        self.assertEqual(state.offset, 0)
        self.assertFalse(state.has_more)
        self.assertFalse(state.loading)
        self.assertIsNone(state.selected_item_id)
        self.assertIsNone(state.kind)
        self.assertFalse(state.favorite)
        self.assertFalse(state.recent)
        self.assertEqual(state.sort, "oldest")


class LibraryRecordTests(unittest.TestCase):
    def test_library_item_supports_attributes_and_legacy_mapping_access(self):
        values = {
            "id": 1,
            "kind": "image",
            "title": "example",
            "content": None,
            "path": "C:/example.png",
            "resolved_path": "C:/example.png",
            "mime": "image/png",
            "content_hash": "a" * 64,
            "created_at": "2026-09-20T00:00:00+00:00",
            "updated_at": None,
            "file_size": 12,
            "width": 2,
            "height": 3,
            "source": "import",
            "favorite": 0,
            "notes": "",
            "ocr_text": "",
            "ai_description": "",
            "embedding": None,
            "embedding_provider": None,
            "embedding_model": None,
            "embedding_dimensions": None,
            "embedding_revision": None,
            "collection_id": None,
            "external": 0,
            "missing": 0,
            "collection_name": None,
            "tag_names": None,
            "tag_colors": None,
        }

        item = LibraryItem.from_mapping(values)

        self.assertEqual(item.kind, "image")
        self.assertEqual(item["kind"], "image")
        self.assertEqual(set(item.keys()), set(values))

    def test_collection_and_tag_summaries_keep_mapping_compatibility(self):
        collection = CollectionSummary.from_mapping(
            {"id": 1, "name": "Inbox", "created_at": "now", "amount": 2}
        )
        tag = TagSummary.from_mapping(
            {"id": 2, "name": "Work", "color": "#fff", "amount": 3}
        )

        self.assertEqual(collection.name, collection["name"])
        self.assertEqual(tag.color, tag["color"])
