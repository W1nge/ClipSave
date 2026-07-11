# ClipSave

ClipSave 是一款面向 Windows 的本地剪贴板资料库，用于自动保存、浏览和整理文字、图片与 Markdown。

## 功能

- 自动捕获剪贴板文字与图片，内容按日期浏览
- 图片网格、列表视图、搜索、排序和收藏
- 集合、标签和备注整理
- Markdown 只读渲染与本地文件定位
- Windows 本地 OCR，中英文内容可参与搜索
- 可折叠导航栏、按需展开详情栏和 Windows 亚克力效果
- 托盘常驻、单实例和 `Ctrl+Alt+V` 全局唤醒
- 可选的 OpenAI-compatible 图片描述与语义搜索

## 本地数据边界

自动捕获、普通搜索、Markdown 阅读和 OCR 都在本机完成：

```text
%LOCALAPPDATA%\ClipSave\Library   剪贴板文件
%LOCALAPPDATA%\ClipSave\Data      数据库、设置和缓存
```

程序目录不会保存用户剪贴板文件。手动导入的图片和 Markdown 会复制到本地资料库，原文件保持不变。

在线 AI 是独立的主动功能，只会在用户配置服务并点击对应命令后运行。详细边界见 [SECURITY.md](SECURITY.md)。

## 环境要求

- Windows 10 / 11
- Python 3.11 或更高版本

## 从源码运行

```powershell
git clone https://github.com/W1nge/ClipSave.git
cd ClipSave
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe clipsave.py
```

也可以运行 `install.bat` 安装依赖，再双击 `run.vbs`。

## 构建 EXE

```powershell
.\build.bat
```

构建产物为项目根目录下的 `ClipSave.exe`，该文件不会提交到 Git。

## 快捷键

| 快捷键 | 操作 |
| --- | --- |
| `Ctrl+K` / `Ctrl+F` | 聚焦搜索框 |
| `Ctrl+B` | 展开或收起左侧栏 |
| `Ctrl+I` | 展开或收起详情栏 |
| `Ctrl+Alt+V` | 从任意位置唤醒 ClipSave |

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 开源许可

[MIT License](LICENSE)
