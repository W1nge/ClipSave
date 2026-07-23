# ClipSave

[English](README.md) | 简体中文

ClipSave 是一款面向 Windows 的本地剪贴板资料库，用于自动保存、浏览和整理文字、图片与 Markdown。

## 功能

- 自动捕获剪贴板文字与图片，并将 Windows 文件复制操作提取为本地路径文字，内容按日期浏览
- 图片网格、列表视图、搜索、排序和收藏
- 集合、标签和备注整理
- Markdown 只读渲染与本地文件定位
- OpenAI-compatible 视觉模型 OCR，识别结果可参与搜索
- 可折叠导航栏、按需展开详情栏和 Windows 亚克力效果
- 托盘常驻、单实例和 `Ctrl+Alt+V` 全局唤醒
- 可选的 OpenAI-compatible 图片描述与按需 AI 扩大搜索

## 本地数据边界

自动捕获、普通搜索和 Markdown 阅读都在本机完成。OCR 和图片描述只会在用户主动执行，或开启对应的自动功能后，发送到已配置的视觉模型：

```text
%LOCALAPPDATA%\ClipSave\Library   剪贴板文件
%LOCALAPPDATA%\ClipSave\Data      数据库、设置和缓存
```

程序目录不会保存用户剪贴板文件。手动导入的图片和 Markdown 会复制到本地资料库，原文件保持不变。

在线 AI 是独立的主动功能，只会在用户配置服务并点击对应命令，或开启自动 OCR/描述后运行。自动功能只处理之后新捕获或导入的图片，不会自动处理已有资料库。详细边界见 [SECURITY.md](SECURITY.md)。

### 配置图片 AI

在“设置”中填写服务 Base URL 和视觉模型名称；只有服务要求鉴权时才需要填写 API Key。“图片自动 OCR”和“图片自动生成描述”是两个独立开关，默认关闭。开启后，新捕获或导入的图片会在后台处理。OCR 固定发送提示词 `ocr this`；图片描述使用 ClipSave 内置的面向检索的完整提示词。

普通本地搜索范围不足时，可以主动点击“扩大搜索”。ClipSave 只会把当前搜索词发送给已配置的模型，再将模型返回的同义词和相关表达以 OR 方式匹配本机数据库中的标题、正文、标签、备注、OCR 和 AI 描述。扩大搜索不会向模型发送资料库条目，也不需要向量模型或向量索引。

新安装默认暂停自动捕获，点击右上角红色状态点可开启。已有有效设置会保持原来的捕获状态；设置文件损坏时会以暂停状态恢复。

## 环境要求

- Windows 10 / 11
- Python 3.11、3.12 或 3.13

## 从源码运行

```powershell
git clone https://github.com/W1nge/ClipSave.git
cd ClipSave
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-windows.lock
.\.venv\Scripts\python.exe clipsave.py
```

也可以运行 `install.bat` 安装并校验依赖，然后使用 `.venv\Scripts\pythonw.exe clipsave.py` 启动源码版本。

发布包用户应直接运行 `ClipSave\ClipSave.exe`，并保持相邻的 `ClipSave\_internal` 目录完整。

## 构建 EXE

```powershell
.\build.bat
```

`build.bat` 固定使用 PyInstaller 6.21.0，并在依赖检查通过后生成 `build\release\ClipSave\` 应用目录和版本化 ZIP。只有设置 `CLIPSAVE_OFFICIAL_BUILD=1`、使用规定的官方 CPython、干净 Git 工作区且安装分发包与哈希锁完全一致时，才会生成 `ClipSave-<version>-windows-x64.zip`；其他本地构建会标记为 `UNOFFICIAL` 并使用不同文件名。Qt DLL 和插件位于 `_internal` 目录；构建失败时脚本会返回非零退出码并清理不完整发布目录。

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

安全边界和已知限制见 [SECURITY.md](SECURITY.md)，版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 本地资料库维护

默认命令只扫描并在 `%LOCALAPPDATA%\ClipSave\Data\maintenance` 生成清单，不删除文件：

```powershell
.\.venv\Scripts\python.exe clipsave_maintenance.py
```

清理命令只处理清单中与数据库有效文件哈希完全一致的副本，并在操作前重新验证文件。回收站和永久删除都要求显式确认短语；独立未索引文件不会自动删除。

## 开源许可

[MIT License](LICENSE)

项目所用第三方组件及其上游许可、Qt 动态库和源码获取信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。该清单用于提供发布信息，不构成法律意见或许可结论。
