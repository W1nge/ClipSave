LIGHT_STYLESHEET = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #172033;
}
QMainWindow, QWidget#AppRoot { background: transparent; }
QDialog#FluentDialog { background: #f6f6f6; border: 1px solid rgba(105,121,145,55); }
QDialog#FluentDialog[dateDialog="true"] { border: 1px solid #000000; }
QWidget#DialogContent { background: #f6f6f6; }
QFrame#DialogTitleBar { background: #f6f6f6; border-bottom: 1px solid rgba(115,129,150,38); }
QFrame#DialogFooter { background: #f6f6f6; border-top: 1px solid rgba(115,129,150,38); }
QScrollArea#DialogScroll { background: #f6f6f6; border: 0; }
QListWidget#DateList { background: transparent; border: 0; outline: 0; }
QListWidget#DateList::item { padding: 0 12px; border-radius: 4px; background: #ffffff; }
QListWidget#DateList::item:hover { background: rgba(39,75,125,16); }
QListWidget#DateList::item:selected { background: rgba(47,125,246,28); color: #135fc7; }
QWidget#WindowBody { background: transparent; }
QFrame#WindowTitleBar { background: rgba(255, 255, 255, 204); border: 0; }
QWidget#Sidebar { background: rgba(255, 255, 255, 204); border: 0; }
QScrollArea#SidebarClassificationScroll, QScrollArea#SidebarClassificationScroll > QWidget > QWidget { background: transparent; border: 0; }
QWidget#ContentSurface { background: transparent; }
QWidget#DetailPanel { background: #f6f6f6; border: 0; }
QFrame#TopBar { background: rgba(255, 255, 255, 204); border-bottom: 1px solid rgba(115,129,150,38); }
QFrame#LibraryHeader { background: #f6f6f6; }
QStackedWidget#ViewStack { background: #f6f6f6; }
QFrame#Card { background: #ffffff; border: 1px solid rgba(105,121,145,40); border-radius: 7px; }
QFrame#Card:hover { background: #ffffff; border-color: rgba(47,125,246,100); }
QFrame#Card[selected="true"] { background: rgba(234,243,255,245); border: 2px solid #2f7df6; }
QFrame#TagChip { background: rgba(236,240,246,210); border-radius: 6px; }
QLabel#Muted { color: #6f7b8d; }
QLabel#SectionTitle { font-size: 15px; font-weight: 600; color: #273247; }
QLabel#Title { font-size: 17px; font-weight: 600; }
QWidget#SettingsSectionHeader { min-height: 24px; background: transparent; }
QLabel#SettingsSectionIcon { background: transparent; }
QLabel#SettingsCaption { color: #6f7b8d; font-size: 12px; }
QLabel#SettingsPath { color: #273247; font-size: 12px; }
QFrame#SettingsDivider, QFrame#SettingsColumnDivider { background: rgba(105,121,145,42); border: 0; }
QWidget#SettingsColumn, QWidget#SettingsField { background: transparent; }
QProgressBar#BulkImageProgress { min-height: 8px; max-height: 8px; background: #e1e6ed; border: 0; border-radius: 0; text-align: center; color: transparent; }
QProgressBar#BulkImageProgress::chunk { background: #2f7df6; border-radius: 0; }
QLineEdit, QTextEdit, QTextBrowser, QComboBox {
    background: #ffffff;
    border: 1px solid rgba(99,115,139,55);
    border-radius: 6px;
    padding: 7px 34px 7px 10px;
    selection-background-color: #2f7df6;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: 0;
    background: transparent;
}
QComboBox::down-arrow { image: none; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #2f7df6; }
QPushButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 7px 10px;
}
QPushButton:hover { background: rgba(39,75,125,18); }
QPushButton:pressed { background: rgba(39,75,125,30); }
QPushButton:disabled { color: rgba(53,64,82,105); background: transparent; }
QPushButton#Primary { background: #2f7df6; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #246be0; }
QPushButton#Danger { background: #c42b1c; color: white; font-weight: 600; }
QPushButton#Danger:hover { background: #a4262c; }
QPushButton#SettingsAction { text-align: left; background: #ffffff; border: 1px solid rgba(99,115,139,38); padding: 9px 12px; }
QPushButton#SettingsAction:hover { background: #ffffff; border-color: rgba(47,125,246,75); }
QPushButton#NavButton { text-align: left; padding: 9px 16px; border-radius: 6px; }
QPushButton#NavButton[active="true"] { background: rgba(47,125,246,18); color: #135fc7; font-weight: 600; }
QPushButton#IconButton { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; padding: 0; }
QPushButton#IconButton[viewSelected="true"] { background: rgba(47,125,246,28); color: #1769d2; }
QPushButton#WindowButton { min-width: 46px; max-width: 46px; min-height: 32px; max-height: 32px; padding: 0; border-radius: 0; }
QPushButton#WindowButton:hover { background: rgba(39,75,125,24); }
QPushButton#CloseWindowButton { min-width: 46px; max-width: 46px; min-height: 32px; max-height: 32px; padding: 0; border-radius: 0; }
QPushButton#CloseWindowButton:hover { background: #c42b1c; }
QPushButton#CaptureStatus { min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; padding: 0; border-radius: 6px; }
QPushButton#CaptureStatus:hover { background: rgba(255,255,255,80); }
QFrame#CopyToast { background: rgba(255,255,255,230); border: 1px solid rgba(76,94,120,70); border-radius: 6px; }
QLabel#CopyToastIcon { background: #2f7df6; border-radius: 17px; }
QLabel#CopyToastText { color: #1f2b3d; font-size: 15px; font-weight: 600; }
QScrollArea { border: 0; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QLabel#DetailPreview { background: rgba(235,239,245,160); border-radius: 6px; }
QListView#AssetGrid { background: #f6f6f6; border: 0; outline: 0; }
QTableView#AssetTable { background: #f6f6f6; alternate-background-color: #ffffff; border: 0; gridline-color: transparent; outline: 0; selection-color: #172033; selection-background-color: rgba(47,125,246,30); }
QTableView#AssetTable::item { padding: 8px 10px; border-bottom: 1px solid rgba(115,129,150,24); }
QTableView#AssetTable::item:selected { color: #172033; background: rgba(47,125,246,30); }
QTableWidget { background: #f6f6f6; alternate-background-color: #ffffff; border: 0; gridline-color: transparent; selection-background-color: rgba(47,125,246,30); }
QTableWidget::item { padding: 8px 10px; border-bottom: 1px solid rgba(115,129,150,24); }
QTableWidget::item:selected { color: #172033; background: rgba(47,125,246,30); }
QHeaderView::section { background: rgb(238,242,246); border: 0; border-bottom: 1px solid rgba(115,129,150,40); padding: 8px 10px; font-weight: 600; }
QMenu { background: #fbfcfe; border: 1px solid #d9dee7; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 4px; }
QMenu::item:selected { background: #e9f2ff; }
QToolTip { background: #1f2937; color: white; border: 0; padding: 5px; }
QSplitter::handle { background: transparent; width: 1px; }
QSplitter#ContentSplitter::handle { background: rgba(115,129,150,44); width: 1px; }
QSplitter#ContentSplitter::handle:hover { background: #2f7df6; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(87,101,122,80); border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar#AutoHideScrollBar:vertical { background: #f6f6f6; width: 14px; margin: 0; }
QScrollBar#AutoHideScrollBar::groove:vertical { background: #f6f6f6; }
QScrollBar#AutoHideScrollBar::add-page:vertical, QScrollBar#AutoHideScrollBar::sub-page:vertical { background: #f6f6f6; }
QScrollBar#AutoHideScrollBar::handle:vertical { background: rgba(87,101,122,135); border-radius: 0; min-height: 32px; }
QScrollBar#AutoHideScrollBar::handle:vertical:hover { background: rgba(71,84,103,180); }
QScrollBar#AutoHideScrollBar::add-line:vertical, QScrollBar#AutoHideScrollBar::sub-line:vertical { height: 0; }
"""


DARK_STYLESHEET = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #f2f2f2;
}
QMainWindow, QWidget#AppRoot { background: transparent; }
QDialog#FluentDialog { background: #202020; border: 1px solid #4a4a4a; }
QDialog#FluentDialog[dateDialog="true"] { border: 1px solid #000000; }
QWidget#DialogContent { background: #202020; }
QFrame#DialogTitleBar { background: #202020; border-bottom: 1px solid #3c3c3c; }
QFrame#DialogFooter { background: #202020; border-top: 1px solid #3c3c3c; }
QScrollArea#DialogScroll { background: #202020; border: 0; }
QListWidget#DateList { background: transparent; border: 0; outline: 0; }
QListWidget#DateList::item { padding: 0 12px; border-radius: 4px; background: #292929; }
QListWidget#DateList::item:hover { background: #343434; }
QListWidget#DateList::item:selected { background: #26384d; color: #79c4ff; }
QWidget#WindowBody { background: transparent; }
QFrame#WindowTitleBar { background: rgba(32,32,32,204); border: 0; }
QWidget#Sidebar { background: rgba(32,32,32,204); border: 0; }
QScrollArea#SidebarClassificationScroll, QScrollArea#SidebarClassificationScroll > QWidget > QWidget { background: transparent; border: 0; }
QWidget#ContentSurface { background: transparent; }
QWidget#DetailPanel { background: #202020; border: 0; }
QWidget#DetailPanelContent { background: #202020; }
QFrame#TopBar { background: rgba(32,32,32,204); border-bottom: 1px solid #3c3c3c; }
QFrame#LibraryHeader { background: #202020; }
QStackedWidget#ViewStack { background: #202020; }
QFrame#Card { background: #292929; border: 1px solid #464646; border-radius: 7px; }
QFrame#Card:hover { background: #303030; border-color: #477eb5; }
QFrame#Card[selected="true"] { background: #26384d; border: 2px solid #4da3ff; }
QFrame#TagChip { background: #353535; border-radius: 6px; }
QLabel#Muted { color: #a7adb7; }
QLabel#SectionTitle { font-size: 15px; font-weight: 600; color: #f2f2f2; }
QLabel#Title { font-size: 17px; font-weight: 600; color: #ffffff; }
QWidget#SettingsSectionHeader { min-height: 24px; background: transparent; }
QLabel#SettingsSectionIcon { background: transparent; }
QLabel#SettingsCaption { color: #a7adb7; font-size: 12px; }
QLabel#SettingsPath { color: #f2f2f2; font-size: 12px; }
QFrame#SettingsDivider, QFrame#SettingsColumnDivider { background: #3c3c3c; border: 0; }
QWidget#SettingsColumn, QWidget#SettingsField { background: transparent; }
QProgressBar#BulkImageProgress { min-height: 8px; max-height: 8px; background: #3a3a3a; border: 0; border-radius: 0; text-align: center; color: transparent; }
QProgressBar#BulkImageProgress::chunk { background: #4da3ff; border-radius: 0; }
QLabel#DetailPreview { background: #303030; border-radius: 6px; }
QLineEdit, QTextEdit, QTextBrowser, QComboBox {
    background: #292929;
    border: 1px solid #505050;
    border-radius: 6px;
    padding: 7px 34px 7px 10px;
    color: #f2f2f2;
    selection-background-color: #2f7df6;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: 0;
    background: transparent;
}
QComboBox::down-arrow { image: none; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #4da3ff; }
QComboBox QAbstractItemView { background: #292929; color: #f2f2f2; selection-background-color: #365a80; }
QPushButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 7px 10px; }
QPushButton:hover { background: rgba(255,255,255,18); }
QPushButton:pressed { background: rgba(255,255,255,28); }
QPushButton:disabled { color: #737373; background: transparent; }
QPushButton#Primary { background: #2f7df6; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #438bf7; }
QPushButton#Danger { background: #d13438; color: white; font-weight: 600; }
QPushButton#Danger:hover { background: #e5484d; }
QPushButton#SettingsAction { text-align: left; background: #292929; border: 1px solid #4a4a4a; padding: 9px 12px; }
QPushButton#SettingsAction:hover { background: #343434; border-color: #4d8bc7; }
QPushButton#NavButton { text-align: left; padding: 9px 16px; border-radius: 6px; }
QPushButton#NavButton[active="true"] { background: rgba(77,163,255,30); color: #79c4ff; font-weight: 600; }
QPushButton#IconButton { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; padding: 0; }
QPushButton#IconButton[viewSelected="true"] { background: rgba(77,163,255,38); color: #79c4ff; }
QPushButton#WindowButton { min-width: 46px; max-width: 46px; min-height: 32px; max-height: 32px; padding: 0; border-radius: 0; }
QPushButton#WindowButton:hover { background: rgba(255,255,255,18); }
QPushButton#CloseWindowButton { min-width: 46px; max-width: 46px; min-height: 32px; max-height: 32px; padding: 0; border-radius: 0; }
QPushButton#CloseWindowButton:hover { background: #c42b1c; }
QPushButton#CaptureStatus { min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; padding: 0; border-radius: 6px; }
QPushButton#CaptureStatus:hover { background: rgba(255,255,255,18); }
QFrame#CopyToast { background: rgba(40,40,40,230); border: 1px solid rgba(255,255,255,48); border-radius: 6px; }
QLabel#CopyToastIcon { background: #2f7df6; border-radius: 17px; }
QLabel#CopyToastText { color: #f7f7f7; font-size: 15px; font-weight: 600; }
QScrollArea { border: 0; background: #202020; }
QScrollArea > QWidget > QWidget { background: #202020; }
QListView#AssetGrid { background: #202020; border: 0; outline: 0; }
QTableView#AssetTable { background: #202020; alternate-background-color: #252525; border: 0; gridline-color: transparent; outline: 0; selection-color: #f2f2f2; selection-background-color: #26384d; }
QTableView#AssetTable::item { padding: 8px 10px; border-bottom: 1px solid #343434; }
QTableView#AssetTable::item:selected { color: #f2f2f2; background: #26384d; }
QHeaderView::section { background: #282828; border: 0; border-bottom: 1px solid #404040; padding: 8px 10px; font-weight: 600; }
QMenu { background: #292929; border: 1px solid #4a4a4a; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 4px; }
QMenu::item:selected { background: #365a80; }
QToolTip { background: #f2f2f2; color: #202020; border: 0; padding: 5px; }
QSplitter::handle { background: transparent; width: 1px; }
QSplitter#ContentSplitter::handle { background: #3c3c3c; width: 1px; }
QSplitter#ContentSplitter::handle:hover { background: #4da3ff; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #666666; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar#AutoHideScrollBar:vertical { background: #202020; width: 14px; margin: 0; }
QScrollBar#AutoHideScrollBar::groove:vertical { background: #202020; }
QScrollBar#AutoHideScrollBar::add-page:vertical, QScrollBar#AutoHideScrollBar::sub-page:vertical { background: #202020; }
QScrollBar#AutoHideScrollBar::handle:vertical { background: #777777; border-radius: 0; min-height: 32px; }
QScrollBar#AutoHideScrollBar::handle:vertical:hover { background: #969696; }
QScrollBar#AutoHideScrollBar::add-line:vertical, QScrollBar#AutoHideScrollBar::sub-line:vertical { height: 0; }
"""
