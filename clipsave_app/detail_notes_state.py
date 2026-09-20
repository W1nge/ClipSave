from __future__ import annotations


class DetailNotesState:
    """Own loaded-note baseline plus unsaved draft/CAS base state."""

    def __init__(self) -> None:
        self.loaded_notes = ""
        self.drafts: dict[int, str] = {}
        self.bases: dict[int, str] = {}

    def display_notes(self, item_id: int, loaded_notes: str) -> str:
        self.loaded_notes = loaded_notes
        return self.drafts.get(item_id, loaded_notes)

    def clear_current(self) -> None:
        self.loaded_notes = ""

    def stage(self, item_id: int, notes: str) -> bool:
        if notes == self.loaded_notes:
            self.drafts.pop(item_id, None)
            self.bases.pop(item_id, None)
            return False
        self.bases.setdefault(item_id, self.loaded_notes)
        self.drafts[item_id] = notes
        return True

    def mark_saved(
        self,
        item_id: int,
        notes: str,
        *,
        update_loaded: bool,
    ) -> None:
        if update_loaded:
            self.loaded_notes = notes
        if self.drafts.get(item_id) == notes:
            self.drafts.pop(item_id, None)
            self.bases.pop(item_id, None)

    def pending_drafts(self) -> dict[int, str]:
        return dict(self.drafts)

    def pending_updates(self) -> dict[int, tuple[str, str]]:
        return {
            item_id: (self.bases.get(item_id, ""), notes)
            for item_id, notes in self.drafts.items()
        }
