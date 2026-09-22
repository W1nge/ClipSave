from __future__ import annotations
from PySide6 import QtCore as _QtCore, QtGui as _QtGui
from PySide6.QtGui import QIcon

from . import asset_grid as _asset_grid
from . import asset_grid_delegate as _asset_grid_delegate
from . import asset_grid_transition as _asset_grid_transition
from . import asset_table as _asset_table
from . import constants as _constants
from . import dialogs as _dialogs
from . import item_gestures as _item_gestures
from . import item_models as _item_models
from . import markdown_view as _markdown_view
from . import thumbnail_service as _thumbnail_service
from . import ui_primitives as _ui_primitives
from .detail_panel import DetailPanel as _BaseDetailPanel
from .sidebar import Sidebar as _BaseSidebar


AssetItemModel = _item_models.AssetItemModel
format_local_timestamp = _item_models.format_local_timestamp
TYPE_LABELS = _constants.TYPE_LABELS
_ItemRightClickGesture = _item_gestures._ItemRightClickGesture
_ItemTripleClickGesture = _item_gestures._ItemTripleClickGesture


_BaseThumbnailDecodeQueue = _thumbnail_service.ThumbnailDecodeQueue
_THUMBNAIL_CACHE = _thumbnail_service._THUMBNAIL_CACHE
_THUMBNAIL_CACHE_LIMIT = _thumbnail_service._THUMBNAIL_CACHE_LIMIT
_THUMBNAIL_REQUEST_LIMIT = _thumbnail_service._THUMBNAIL_REQUEST_LIMIT
_THUMBNAIL_SIZE = _thumbnail_service._THUMBNAIL_SIZE
_THUMBNAIL_WORKERS = _thumbnail_service._THUMBNAIL_WORKERS
_ThumbnailCacheKey = _thumbnail_service._ThumbnailCacheKey
_ThumbnailDecodeSignals = _thumbnail_service._ThumbnailDecodeSignals
_ThumbnailDecodeTask = _thumbnail_service._ThumbnailDecodeTask
_ThumbnailPixmapCache = _thumbnail_service._ThumbnailPixmapCache
_cache_decoded_thumbnail = _thumbnail_service._cache_decoded_thumbnail
_cached_thumbnail = _thumbnail_service._cached_thumbnail
_base_decode_thumbnail_image = _thumbnail_service._decode_thumbnail_image
_thumbnail_cache_key = _thumbnail_service._thumbnail_cache_key
thumbnail_pixmap = _thumbnail_service.thumbnail_pixmap
AssetTable = _asset_table.AssetTable
_AssetTableContentDelegate = _asset_table._AssetTableContentDelegate
AssetGridDelegate = _asset_grid_delegate.AssetGridDelegate
_GridTransitionPreviewState = _asset_grid_transition._GridTransitionPreviewState
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
_settings_section_header = _dialogs._settings_section_header
_settings_field = _dialogs._settings_field
_startfile_or_warn = _dialogs._startfile_or_warn
_SafeMarkdownBrowser = _markdown_view._SafeMarkdownBrowser
MAX_RICH_MARKDOWN_BYTES = _markdown_view.MAX_RICH_MARKDOWN_BYTES
_set_markdown_content = _markdown_view._set_markdown_content

GLYPHS = _ui_primitives.GLYPHS
lucide_icon = _ui_primitives.lucide_icon
dark_theme_active = _ui_primitives.dark_theme_active
theme_icon_color = _ui_primitives.theme_icon_color
_ThemedTextContextMenuMixin = _ui_primitives._ThemedTextContextMenuMixin
ThemedLineEdit = _ui_primitives.ThemedLineEdit
ThemedTextEdit = _ui_primitives.ThemedTextEdit
ThemedSelectableLabel = _ui_primitives.ThemedSelectableLabel
friendly_day = _ui_primitives.friendly_day
color_dot = _ui_primitives.color_dot
IconButton = _ui_primitives.IconButton
FluentComboBox = _ui_primitives.FluentComboBox
ToggleSwitch = _ui_primitives.ToggleSwitch
ResizeHandle = _ui_primitives.ResizeHandle
BrandLabel = _ui_primitives.BrandLabel
CaptureStatusButton = _ui_primitives.CaptureStatusButton
CopyToast = _ui_primitives.CopyToast
AutoHideScrollBar = _ui_primitives.AutoHideScrollBar
_WheelRemainder = _ui_primitives._WheelRemainder
_half_speed_wheel_event = _ui_primitives._half_speed_wheel_event
WindowTitleBar = _ui_primitives.WindowTitleBar
DraggableBar = _ui_primitives.DraggableBar
_available_dialog_size = _ui_primitives._available_dialog_size
_fit_dialog_size = _ui_primitives._fit_dialog_size
DialogTitleBar = _ui_primitives.DialogTitleBar
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
