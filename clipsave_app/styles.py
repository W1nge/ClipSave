LIGHT_STYLESHEET = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #172033;
}
QMainWindow, QWidget#AppRoot { background: transparent; }
QWidget#Sidebar { background: rgba(255, 255, 255, 204); border-right: 1px solid rgba(115, 129, 150, 42); }
QWidget#ContentSurface { background: transparent; }
QWidget#DetailPanel { background: rgba(249, 251, 254, 248); border-left: 1px solid rgba(115, 129, 150, 44); }
QFrame#TopBar { background: rgba(255, 255, 255, 204); border-bottom: 1px solid rgba(115,129,150,38); }
QFrame#LibraryHeader { background: rgba(249, 251, 254, 250); }
QStackedWidget#ViewStack { background: rgba(249, 251, 254, 250); }
QFrame#Card { background: rgba(255,255,255,218); border: 1px solid rgba(105,121,145,40); border-radius: 7px; }
QFrame#Card:hover { background: rgba(255,255,255,245); border-color: rgba(47,125,246,100); }
QFrame#Card[selected="true"] { background: rgba(234,243,255,245); border: 2px solid #2f7df6; }
QFrame#TagChip { background: rgba(236,240,246,210); border-radius: 6px; }
QLabel#Muted { color: #6f7b8d; }
QLabel#SectionTitle { font-size: 15px; font-weight: 600; color: #273247; }
QLabel#Title { font-size: 17px; font-weight: 600; }
QLineEdit, QTextEdit, QTextBrowser, QComboBox {
    background: rgba(255,255,255,190);
    border: 1px solid rgba(99,115,139,55);
    border-radius: 6px;
    padding: 7px 10px;
    selection-background-color: #2f7df6;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #2f7df6; }
QPushButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 7px 10px;
}
QPushButton:hover { background: rgba(39,75,125,18); }
QPushButton:pressed { background: rgba(39,75,125,30); }
QPushButton#Primary { background: #2f7df6; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #246be0; }
QPushButton#NavButton { text-align: left; padding: 9px 12px; border-radius: 6px; }
QPushButton#NavButton[active="true"] { background: rgba(47,125,246,18); color: #135fc7; font-weight: 600; }
QPushButton#IconButton { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; padding: 0; }
QPushButton#CaptureStatus { min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; padding: 0; border-radius: 6px; }
QPushButton#CaptureStatus:hover { background: rgba(255,255,255,80); }
QScrollArea { border: 0; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QTableWidget { background: transparent; border: 0; gridline-color: rgba(115,129,150,35); selection-background-color: rgba(47,125,246,30); }
QHeaderView::section { background: rgba(244,247,251,220); border: 0; border-bottom: 1px solid rgba(115,129,150,40); padding: 8px; font-weight: 600; }
QMenu { background: #fbfcfe; border: 1px solid #d9dee7; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 4px; }
QMenu::item:selected { background: #e9f2ff; }
QToolTip { background: #1f2937; color: white; border: 0; padding: 5px; }
QSplitter::handle { background: transparent; width: 1px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(87,101,122,80); border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
