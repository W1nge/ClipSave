from __future__ import annotations
from PySide6 import QtCore as _QtCore, QtGui as _QtGui
from PySide6.QtGui import QIcon

from . import asset_grid as _asset_grid
from . import asset_grid_delegate as _asset_grid_delegate
from . import asset_grid_transition as _asset_grid_transition
from . import asset_table as _asset_table
from . import dialogs as _dialogs
from . import markdown_view as _markdown_view
from . import thumbnail_service as _thumbnail_service
from . import ui_primitives as _ui_primitives
from .detail_panel import DetailPanel as _BaseDetailPanel
from .sidebar import Sidebar as _BaseSidebar




_BaseThumbnailDecodeQueue = _thumbnail_service.ThumbnailDecodeQueue
_THUMBNAIL_CACHE = _thumbnail_service._THUMBNAIL_CACHE
_THUMBNAIL_REQUEST_LIMIT = _thumbnail_service._THUMBNAIL_REQUEST_LIMIT
_THUMBNAIL_WORKERS = _thumbnail_service._THUMBNAIL_WORKERS
_ThumbnailCacheKey = _thumbnail_service._ThumbnailCacheKey
_cache_decoded_thumbnail = _thumbnail_service._cache_decoded_thumbnail
_cached_thumbnail = _thumbnail_service._cached_thumbnail
_base_decode_thumbnail_image = _thumbnail_service._decode_thumbnail_image
_thumbnail_cache_key = _thumbnail_service._thumbnail_cache_key
thumbnail_pixmap = _thumbnail_service.thumbnail_pixmap
AssetTable = _asset_table.AssetTable
AssetGridDelegate = _asset_grid_delegate.AssetGridDelegate
_GridTransitionCard = _asset_grid_transition._GridTransitionCard
_grid_transition_rect = _asset_grid_transition._grid_transition_rect
_grid_transition_card_elevated = _asset_grid_transition._grid_transition_card_elevated
_interpolate_rect = _asset_grid_transition._interpolate_rect
_AssetGridTransitionOverlay = _asset_grid_transition._AssetGridTransitionOverlay
QRectF = _QtCore.QRectF
QTextOption = _QtGui.QTextOption
MarkdownDialog = _dialogs.MarkdownDialog
TextDialog = _dialogs.TextDialog
DateDialog = _dialogs.DateDialog
SettingsDialog = _dialogs.SettingsDialog
os = _dialogs.os
QMessageBox = _dialogs.QMessageBox
_startfile_or_warn = _dialogs._startfile_or_warn
_SafeMarkdownBrowser = _markdown_view._SafeMarkdownBrowser
MAX_RICH_MARKDOWN_BYTES = _markdown_view.MAX_RICH_MARKDOWN_BYTES

lucide_icon = _ui_primitives.lucide_icon
ThemedLineEdit = _ui_primitives.ThemedLineEdit
IconButton = _ui_primitives.IconButton
FluentComboBox = _ui_primitives.FluentComboBox
ResizeHandle = _ui_primitives.ResizeHandle
BrandLabel = _ui_primitives.BrandLabel
CaptureStatusButton = _ui_primitives.CaptureStatusButton
CopyToast = _ui_primitives.CopyToast
AutoHideScrollBar = _ui_primitives.AutoHideScrollBar
_WheelRemainder = _ui_primitives._WheelRemainder
_half_speed_wheel_event = _ui_primitives._half_speed_wheel_event
WindowTitleBar = _ui_primitives.WindowTitleBar
DraggableBar = _ui_primitives.DraggableBar
_fit_dialog_size = _ui_primitives._fit_dialog_size
FluentMessageDialog = _ui_primitives.FluentMessageDialog
FluentMessageBox = _ui_primitives.FluentMessageBox


class NavButton(_ui_primitives.NavButton):
    """Compatibility facade keeping widgets.lucide_icon patchable."""

    def _render_lucide_icon(self, glyph: str, color: str) -> QIcon:
        return lucide_icon(glyph, color)


class Sidebar(_BaseSidebar):
    """Compatibility facade keeping widgets.NavButton behavior injectable."""

    def _nav_button(self, *args, **kwargs) -> NavButton:
        return NavButton(*args, **kwargs)


class DetailPanel(_BaseDetailPanel):
    """Compatibility facade keeping widgets thumbnail/icon seams injectable."""

    def _make_thumbnail_queue(self, parent):
        return _ThumbnailDecodeQueue(parent)

    def _thumbnail_lookup(self, path, content_hash=None):
        return _cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return _cache_decoded_thumbnail(key, image)

    def _icon(self, name: str) -> QIcon:
        return lucide_icon(name)


class AssetGrid(_asset_grid.AssetGrid):
    """Compatibility facade keeping widgets thumbnail seams injectable."""

    def _make_thumbnail_queue(self, parent):
        return _ThumbnailDecodeQueue(parent)

    def _thumbnail_lookup(self, path, content_hash=None):
        return _cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return _cache_decoded_thumbnail(key, image)






_decode_thumbnail_image = _base_decode_thumbnail_image


class _ThumbnailDecodeQueue(_BaseThumbnailDecodeQueue):
    """Compatibility queue keeping widgets._decode_thumbnail_image patchable."""

    def __init__(
        self,
        parent=None,
        *,
        max_workers: int = _THUMBNAIL_WORKERS,
        max_requests: int = _THUMBNAIL_REQUEST_LIMIT,
    ):
        super().__init__(
            parent,
            max_workers=max_workers,
            max_requests=max_requests,
            decode_image=lambda key: _decode_thumbnail_image(key),
        )
