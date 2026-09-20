"""Compatibility exports for ClipSave's shared UI building blocks."""

from __future__ import annotations

from . import ui_controls as _controls
from . import window_chrome as _chrome


GLYPHS = _controls.GLYPHS
lucide_icon = _controls.lucide_icon
dark_theme_active = _controls.dark_theme_active
theme_icon_color = _controls.theme_icon_color
_TEXT_MENU_ACTION_ICONS = _controls._TEXT_MENU_ACTION_ICONS
ThemedTextContextMenuMixin = _controls.ThemedTextContextMenuMixin
_ThemedTextContextMenuMixin = _controls._ThemedTextContextMenuMixin
ThemedLineEdit = _controls.ThemedLineEdit
ThemedTextEdit = _controls.ThemedTextEdit
ThemedSelectableLabel = _controls.ThemedSelectableLabel
friendly_day = _controls.friendly_day
color_dot = _controls.color_dot
IconButton = _controls.IconButton
FluentComboBox = _controls.FluentComboBox
ToggleSwitch = _controls.ToggleSwitch
ResizeHandle = _controls.ResizeHandle
BrandLabel = _controls.BrandLabel
CaptureStatusButton = _controls.CaptureStatusButton
CopyToast = _controls.CopyToast
AutoHideScrollBar = _controls.AutoHideScrollBar
WheelRemainder = _controls.WheelRemainder
_WheelRemainder = _controls._WheelRemainder
half_speed_wheel_event = _controls.half_speed_wheel_event
_half_speed_wheel_event = _controls._half_speed_wheel_event
NavButton = _controls.NavButton

WindowTitleBar = _chrome.WindowTitleBar
DraggableBar = _chrome.DraggableBar
_available_dialog_size = _chrome._available_dialog_size
_fit_dialog_size = _chrome._fit_dialog_size
fit_dialog_size = _chrome.fit_dialog_size
DialogTitleBar = _chrome.DialogTitleBar
FluentMessageDialog = _chrome.FluentMessageDialog
FluentMessageBox = _chrome.FluentMessageBox
