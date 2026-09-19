# ClipSave 1.1.2 第二轮实现审计报告

日期：2026-09-19
审计分支：`cleanup/1.1.2`
审计基线：`a6502bd Simplify code for 1.1.2`

> **实施状态更新（同日）**：本报告正文记录的是修复前审计基线。报告完成后已经进入第二轮落地阶段。当前工作树已实际处理 H-01～H-05、M-01～M-07、M-09～M-10、M-12～M-13，以及 L-01～L-04 的主要问题；M-08 的列表页双 tag 聚合仍保留，FTS5 与更深的模块边界重构暂缓到有独立 benchmark / 迁移计划后再做。

---

## 1. 审计目标

第一轮审计的目标是“确认并删除真正无用的代码”。第二轮不再局限于死代码，而是把范围扩大到整个实现质量：

- 真实逻辑错误与边界条件；
- 重复实现和已经发生行为漂移的代码；
- I/O、数据库、图片解码、线程和 UI 主线程性能问题；
- 异步任务、取消、退出、资源生命周期；
- Windows 10 Acrylic / 原生窗口桥；
- 文件系统安全、数据库一致性和迁移；
- 构建、安装、发布和 smoke / 性能验证工具；
- 现有测试覆盖不到、但实现本身仍然脆弱的路径。

本报告生成时**没有修改产品代码**；其后的实施阶段已经按本报告逐项落地。正文保留原始发现与建议，便于对照修复前基线。

---

## 2. 审计方法与验证基线

本轮实际执行了：

1. 逐模块阅读核心实现，重点覆盖：
   - `database.py`
   - `storage.py`
   - `services.py`
   - `main_window.py`
   - `widgets.py`
   - `app.py`
   - `windows_frame.py`
   - `windows_backdrop.py`
   - NativeAOT C# backdrop bridge
   - maintenance / build / installer / GitHub Release 工具链
2. AST 复杂度热点扫描；
3. AST 精确结构克隆扫描；
4. Pyflakes 静态检查；
5. 搜索调用链、测试调用点和生产调用点；
6. 重新运行完整测试；
7. 构造 20,000 条记录的临时 SQLite 资料库，对当前搜索路径做实际计时。

当前基线：

- `python -m compileall`：PASS
- `git diff --check`：PASS
- 完整 unittest：**532 / 532 PASS**
- 当前应用源代码工作树在写报告前保持 clean。

这意味着下面很多问题不是“测试已经失败”，而是：

> **现有测试证明了当前已覆盖行为没有回归，但不能证明实现没有性能墙、生命周期脆弱性或验证工具盲区。**

---

## 3. 总结

### 3.1 没有发现 P0 级灾难

没有发现“正常使用立即导致数据库损坏、大范围误删文件、稳定崩溃”的 P0 问题。

第一轮保留下来的文件身份校验、reparse / hardlink 防护、删除前 hash 校验、数据库事务、备份与 Win10 Acrylic 几何同步总体是认真且防御性很强的。

### 3.2 第二轮最值得处理的是五类问题

1. **数据库查询在 GUI 主线程同步执行，已经能量化出数百毫秒卡顿。**
2. **文件修复和图片导入存在显著的重复全文件 hash / decode，I/O 放大明显。**
3. **退出流程有两套大型状态机，代码已经发生明显行为漂移。**
4. **Acrylic 交互性能 gate 有两个验证漏洞：backend 检查通常被跳过，单个调度尖峰又会导致偶发误报。**
5. **AI/OCR、批处理和 smoke / release 工具中存在若干真实但尚未被测试完整覆盖的边界问题。**

---

## 4. 高优先级发现

## H-01：资料库搜索与刷新同步阻塞 GUI 主线程

**位置**

- `MainWindow.refresh_items()`
- `MainWindow.load_more_items()`
- `MainWindow._refresh_navigation_metadata()`
- `LibraryDatabase.query_items()`

**事实**

`refresh_items()` 直接在 GUI 线程调用：

```python
items = self._query_current_items(self.ITEM_PAGE_SIZE, 0)
```

`ITEM_PAGE_SIZE = 500`。

搜索 SQL 对每个 term 使用：

- title `LIKE %term%`
- content `LIKE %term%`
- ocr_text `LIKE %term%`
- ai_description `LIKE %term%`
- notes `LIKE %term%`
- tag `EXISTS ... LIKE %term%`

扩大搜索最多允许 **16 个 term**。

由于前导 `%`，普通 B-Tree 索引无法解决正文扫描；而且这一切都发生在 GUI 线程。

**实测**

本轮创建了 20,000 条、每条约 1 KB 文本的临时数据库，在当前实现上重复查询：

| 场景 | 实测 |
|---|---:|
| 无搜索，取首屏 500 条 | 138.1–143.5 ms |
| 单个搜索词 | 51.3–52.0 ms |
| 16 个扩大搜索词 | 471.2–478.7 ms |
| counts + collections + tags + collections | 49.9 ms |

这不是数据库极限 benchmark，只是为了验证 UI 路径是否已经进入人可感知卡顿区。答案是肯定的。

**影响**

- 搜索输入后刷新可能明显停顿；
- 切换标签/集合/排序可能停顿；
- 捕获、删除、备注保存等触发 `refresh_library()` 的操作也会同步做多组查询；
- 数据越大问题越明显。

**建议**

短期：

1. 把查询移出 GUI 线程；
2. 搜索使用 generation/token，只接受最新结果；
3. `_refresh_navigation_metadata()` 一次读取 collections 后同时喂给 sidebar/detail，不要查询两次。

中期：

4. 为全文字段引入 SQLite FTS5；
5. 扩大搜索不要展开成最多 16 组 `%LIKE%`；
6. 深分页考虑 keyset pagination，而不是无限增长的 OFFSET。

---

## H-02：`mark_missing_files()` 对正常文件重复做完整 SHA-256

**位置**

- `LibraryDatabase.mark_missing_files()`

**当前流程**

第一阶段对每个 indexed file：

1. `path.stat()`
2. `path.is_file()`
3. `self.file_hash(path)` —— 完整读文件一次

之后对 missing/present candidate 又：

4. `storage.open_managed_binary(... identity_locked=True)`
5. `fstat()`
6. `_stream_hash(current_file)` —— 再完整读文件一次

也就是说，一个完全正常、没有变化的已索引文件，在一次 reconcile 中也会完整 hash **两遍**。

SQL 同时选出了 `file_size`，但当前第一轮分类并没有用它减少工作。

**影响**

资料库中图片越多、图片越大，启动扫描/修复扫描越接近纯磁盘带宽任务。

**建议**

直接以“一次 identity-locked snapshot”为权威：

1. 打开安全 handle；
2. `fstat`；
3. 必要时 hash 一次；
4. 同一份结果完成存在性、身份、size、hash 判断；
5. 删除前/变更前再按安全需求做最终校验。

不要先做一遍普通路径 hash，再做一遍 identity-locked hash。

---

## H-03：外部图片导入存在明显的全文件 I/O / decode 放大

**位置**

- `MainWindow.import_files()`
- `preflight_image_file()`
- `LibraryDatabase.import_file()`

**新外部图片的一条典型路径**

UI 层：

1. `preflight_image_file()`：PIL 完整验证/解码；
2. `LibraryDatabase.file_hash(candidate)`：完整 SHA-256。

DB 导入层：

3. identity-locked source hash；
4. 复制 source 时再次完整读取并 hash；
5. 复制完成后重新打开 target 再 hash；
6. 最终 identity handle 再 hash；
7. 再次用 PIL 打开并 `image.load()`。

因此一张新外部图片可以经历：

- **约 5 次完整 hash/copy 级别的数据遍历**
- **2 次完整图片 decode/验证**

其中部分重复检查是为了 TOCTOU 安全，这是合理的；问题在于每一层都重新从零开始，安全结果没有跨层传递。

**影响**

- 大图片批量导入时磁盘吞吐和 CPU 解码被重复消耗；
- NVMe 上可能表现为 CPU/hash 占用，机械盘/网络来源则会更明显；
- 代码也因此形成 249 行、62 个 branch-like 节点的 `import_file()`。

**建议**

引入一个明确的 verified snapshot/result 对象，例如：

```text
VerifiedImportSource
  path
  stat identity
  sha256
  size
  image dimensions
  kind
```

由底层安全代码生成并在导入流程中传递。

UI 不应先自行 hash 再要求 DB 重新 hash。最终落库应返回结构化结果（item_id/status/hash），也就不需要 UI 再通过 hash 查询刚导入的 ID。

---

## H-04：交互 Acrylic gate 并不稳定验证“当前 backend 真的是 Acrylic”

**位置**

- `verify_windows_interactive_backdrop.py`
- `app.py` smoke status 写入流程

验证器启动应用时使用约 30 秒 `--smoke-hold-ms`。

应用的 `ready.status` 主要在 `quit_smoke()` 阶段写入，也就是 hold 时间结束后。

但交互验证通常几秒内已经测完，然后代码是：

```python
if status.exists():
    ...
    if values.get("backdrop_backend") != "win10_effect_acrylic":
        FAIL
```

所以在正常快速完成的 gate 中，`status.exists()` 往往是 false，backend 检查被整个跳过。

**后果**

当前脚本确实能验证：

- helper HWND 存在；
- 几何同步；
- DwmFlush cadence。

但它没有可靠证明测试期间真正 attach 的 material 就是 `win10_effect_acrylic`。

**建议**

在 smoke ready 时立即写一份“ready status”，至少包含：

- backend name；
- backdrop_success；
- host hwnd / helper hwnd（必要时）；
- ready timestamp。

交互 verifier 必须等待这份状态文件，并把 backend 检查变成**必选条件**，不能 `if exists` 后可选执行。

---

## H-05：Acrylic 性能 gate 对单个 scheduler outlier 过度敏感

**位置**

- `verify_windows_interactive_backdrop.py`

当前判定：

```python
if any(value > 33.3 for value in values):
    FAIL
```

即 240 个样本中只要有 **1 个** 超过 33.3 ms，整轮失败。

第一轮最终验证已经实际出现：

- 第一次 resize：仅 1 / 240 超过 33.3 ms → FAIL
- 立即重跑：0 / 240 超过 33.3 ms → PASS
- 两轮 geometry max_delta 都是 0 px

这非常符合 Windows 调度偶发尖峰，而不是 Acrylic 实现发生确定性退化。

**建议**

把 gate 分成两类：

1. **几何正确性**仍然保持严格：任何 geometry delta 都可以 fail；
2. **性能**用统计规则：
   - p95 / p99；
   - >16.7ms 比例；
   - >33.3ms 比例；
   - 连续 stall 次数；
   - 或允许 240 样本中 1 个孤立 outlier。

这样既能抓持续退化，也不会被单个系统调度尖峰污染 release 判断。

---

## 5. 中优先级发现

## M-01：两套退出流程合计 300+ 行，已经发生生命周期行为漂移

**位置**

- `quit_application_for_session_end()`：约 127 行
- `quit_application()`：约 185 行

二者都在做：

- flush UI 状态；
- 禁用交互；
- 停 search；
- 停 startup/import/backup；
- cancel expanded search / AI / OCR / copy / delete；
- 等待普通线程和 bounded executor；
- clipboard drain/shutdown；
- thumbnail shutdown；
- DB close；
- tray/app exit；
- 失败时恢复 UI。

但是细节已经不同：

- 正常退出分别给 startup/import/backup 10 秒；
- session-end 共用一个 deadline；
- 正常退出会做 final backup；
- session-end 不做；
- 正常退出先等 thumbnail idle；
- session-end 直接走剩余 deadline；
- session-end 有单独的 notes daemon thread；
- 两边失败 rollback 逻辑完全不同。

这不是单纯“代码长”，而是典型的**两套状态机长期漂移**。

**建议**

抽成统一 shutdown coordinator/state machine，例如阶段：

```text
FLUSH_UI
PERSIST_NOTES
CANCEL_BACKGROUND
DRAIN_CLIPBOARD
FINAL_BACKUP (policy)
STOP_EXECUTORS
CLOSE_DB
EXIT
```

正常退出与 Windows session-end 只提供 policy：

- deadline；
- 是否允许 UI dialog；
- 是否要求 final backup；
- 是否 processEvents；
- timeout 后如何 rollback。

---

## M-02：session-end 备注保存线程超时后仍继续运行

`quit_application_for_session_end()` 会启动：

```python
threading.Thread(..., daemon=True).start()
notes_finished = note_done.wait(remaining())
```

如果超时，shutdown 被 abort，UI 恢复，但旧的 note writer **没有取消机制**，仍可能继续访问数据库。

现有代码已经用 `set_notes_if_unchanged()` 做 CAS，测试也覆盖了“不能覆盖已经保存到 DB 的 newer edit”，所以最危险的数据覆盖已经被缓解。

但仍有生命周期问题：

- abort 后仍有游离线程；
- 后续再次 shutdown 时旧线程可能仍存在；
- 新 UI 会话已经恢复，旧退出事务仍可能继续落盘；
- `note_saved_ids` 是主线程与 worker 共享的普通 list。

**建议**

不要在 shutdown 函数内部临时创建不可追踪 daemon。

把 note persistence 变成 MainWindow 已登记的 async task / TaskHandle，使其：

- 可取消；
- 可 join；
- 可 generation 隔离；
- abort 后旧结果不再改变当前生命周期状态。

---

## M-03：AI / OCR 两套请求编排高度重复

**位置**

- `generate_ai_description()`
- `generate_ocr()`
- `_ai_succeeded()` / `_ocr_succeeded()`
- `_ai_failed()` / `_ocr_failed()`

两套流程基本一致：

- 取 item；
- preflight；
- hash snapshot；
- cancel previous；
- busy UI；
- signals/token；
- bounded executor；
- require_current；
- service call；
- compare-and-set；
- stale result；
- automatic/manual error behavior。

主要差异只是：

- 调哪个 service method；
- 写哪个 DB 字段；
- UI 文案。

**额外问题**

生产 signal connection 始终会传 `expected_content_hash`，但 success callback 仍允许 `None`，并在 None 时退回非 CAS 的 `update_ai()` / `update_ocr()`。

这条 fallback 主要是测试/历史兼容，生产路径没有必要保留。

**建议**

用 operation descriptor 或共享 helper 合并编排，并让 expected hash 成为必填参数。

---

## M-04：AIService 的“90 秒总 deadline”不是严格总超时

**位置**

- `AIService._post()`

代码设置：

```python
deadline = monotonic() + 90
```

打开连接前会计算 remaining timeout 并传给 urllib。

但是读取 response body 时：

- 每次 read 前会检查 deadline；
- **read 本身的 socket timeout 没有持续缩短为剩余 deadline**。

如果连接阶段已经耗掉很多时间，随后某次 body read 卡住，它仍可能使用较早设置的较长 timeout。

因此“90 秒”更像应用层检查 + socket timeout 的组合，并不是真正严格的 wall-clock total timeout。

现有测试只验证 open timeout 使用 remaining deadline，没有覆盖 body 在 deadline 边界阻塞。

**建议**

- 每次 body read 前把底层 read timeout 更新为 remaining time；或
- 换成具有 connect/read/total timeout 明确语义的 HTTP client；
- 添加慢 body 跨 deadline 的测试。

---

## M-05：批量图片处理为了“计数/ID 列表”加载了整批完整 query rows

**位置**

`_confirm_bulk_image_processing()`：

```python
len(self.database.query_items(kind="image", summary_only=True))
```

只是要 count，却把所有 image row 查回 Python。

`start_bulk_image_processing()`：

```python
[int(row["id"]) for row in self.database.query_items(
    kind="image", sort="oldest", summary_only=True
)]
```

只是要 ID，却仍执行完整 `query_items()` projection，包括 collection name 和 tag 聚合。

**影响**

图片库大时，启动批处理本身会：

- 主线程同步加载大量 row；
- 做无意义的 tag subquery；
- 构造大量 Python sqlite Row；
- 然后才开始真正后台处理。

**建议**

新增：

- `count_items(kind=...)`
- `item_ids(kind=..., order=...)` 或分页 ID iterator。

---

## M-06：Clipboard 图片保存再次重复 hash / decode

**位置**

- `ClipboardService.save_image()`
- `LibraryDatabase.add_image()`

当前保存路径：

1. QImage 编码 PNG 到内存；
2. 写文件；
3. 对内存 PNG 算 hash；
4. 重新打开落盘文件再 hash；
5. `database.add_image()` 再打开文件 hash；
6. PIL 再 decode 获取尺寸/验证。

其中 QImage 的 width/height 已知，PNG payload hash 也已经计算。

**建议**

底层提供“插入已验证 managed image snapshot”的 API。

仍保留最终磁盘身份/内容核验，但不要 DB 层再从零做第三遍同类工作。

---

## M-07：`ImportFileResult.LOCALIZED` 故意伪装成 False

**位置**

`database.py`：

```python
class ImportFileResult(Enum):
    LOCALIZED = "localized"
    def __bool__(self):
        return False
    def __int__(self):
        return 0
```

`import_file()` 的返回类型是：

```python
bool | ImportFileResult
```

“成功本地化”却故意是 falsey，导致调用方必须知道这个陷阱：

```python
if import_result is ImportFileResult.LOCALIZED:
    ...
elif import_result:
    ...
else:
    duplicate
```

代码里甚至已经需要注释提醒它“deliberately evaluates false”。

**风险**

未来任何新的调用方写 `if database.import_file(...):`，都会把成功 LOCALIZED 当失败/重复。

**建议**

统一使用结构化结果：

```text
ImportResult(
    status = ADDED | LOCALIZED | DUPLICATE,
    item_id,
    content_hash,
    path
)
```

不要再用 bool/int 魔术转换表达业务状态。

---

## M-08：`query_items()` / `get_item()` 的 tag SQL 有冗余

`query_items()` 每个返回 row 都独立执行两次相关子查询：

1. GROUP_CONCAT tag names
2. GROUP_CONCAT tag colors

两次都遍历相同的 `item_tags JOIN tags`。

`get_item()` 更明显：

它已经通过两个相关子查询获取 tag_names/tag_colors，但外层又：

```sql
LEFT JOIN item_tags it ON it.item_id = i.id
LEFT JOIN tags t ON t.id = it.tag_id
GROUP BY i.id
```

外层 `it/t` 列没有被使用，主要作用只是制造重复行后再 GROUP BY 回去。

**建议**

- 先删除 `get_item()` 的无效 outer tag join/group by；
- 对列表页考虑一次聚合出 name/color pair，而不是每 row 两个相关子查询；
- 更进一步可把 tags 批量按本页 item IDs 一次取回，在 Python 组装。

---

## M-09：`_refresh_navigation_metadata()` 每次刷新 collections 查询两遍

当前：

```python
self.sidebar.set_collections(self.database.collections())
self.sidebar.set_tags(self.database.tags())
self.detail.set_collections(self.database.collections())
```

同一 GUI refresh 内 `collections()` 完全相同地调用两次。

这是非常明确、低风险可删的冗余。

---

## M-10：smoke ready 达到 80 次仍未 ready 时，应用不会主动失败退出

**位置**

- `app.py::mark_smoke_ready()`

逻辑最多重新 schedule 80 次。

如果第 80 次：

- 没有明确 startup error；
- 也仍不满足 ready；

函数就停止 schedule，但没有：

- 写 failure reason；
- `app.exit(nonzero)`。

于是 smoke app 会继续留在 event loop，直到外部 verifier timeout 后强杀。

**影响**

“应用启动失败”和“测试工具自己悬挂”混在一起，诊断质量下降。

**建议**

第 80 次进入 terminal failure：

- 写 status；
- 带 reason；
- 非 0 exit。

`quit_smoke()` 的无限 retry 也应有自己的 deadline。

---

## M-11：模块之间大量直接调用“私有实现”

典型例子：

- `database.py::_SQLiteLeafLock` 直接调用多个 `storage._xxx` Win32 私有函数；
- `maintenance.py` 直接拿 `database._lock`；
- maintenance 直接调用 `LibraryDatabase._stream_hash`；
- `services.py` 调 `database._path_key`；
- session-end 直接读 `detail._loaded_notes`。

这说明真实模块边界和命名边界不一致。

**后果**

“看起来是 private、可以安全改”的函数，其实可能被别的模块依赖。

第一轮审计之所以需要极其保守，就是这个原因。

**建议**

不用做大架构重写，只需要把真正跨模块使用的能力提升为明确 internal API：

- verified leaf/file identity API；
- canonical path key；
- DetailPanel note snapshot；
- DB locked maintenance helper。

---

## M-12：GitHub Release 文案仍硬编码为 Acrylic release

**位置**

- `.github/workflows/release.yml` → Generate release notes

固定写着：

```text
ClipSave $version focuses on Windows Acrylic quality and interactive window performance.
```

这对 1.1.1 合理，但 1.1.2 的目标已经是代码精简/维护性。

如果现在直接 tag 1.1.2，Release 页面会出现与实际版本目标不一致的固定说明。

**建议**

删除硬编码 release 主题，让 CHANGELOG 成为唯一正文来源；或者每个版本在 changelog front matter/脚本参数中提供 summary。

这是**发布 1.1.2 前应先修**的项目。

---

## M-13：官方 release workflow 没有运行项目已有的 Windows visual/interactive gates

Release workflow 当前执行：

- build；
- unittest；
- artifact validation；
- installer smoke。

但没有执行：

- `verify_windows_visual_smoke.py`
- `verify_windows_interactive_backdrop.py`

考虑到 Acrylic 是该项目最复杂的 Windows 特殊路径之一，这两个工具目前实际上属于“本地人工 release gate”，不是 CI gate。

GitHub Windows runner 是否适合稳定执行 DWM/GUI 性能 gate 需要单独验证；如果 CI 环境不适合，至少应在 release checklist 中明确记录“必须人工运行”，而不是让脚本存在但没有正式门禁归属。

---

## 6. 低优先级 / 可直接精简项

## L-01：两个完全相同的 human-size formatter

AST 精确结构克隆扫描确认：

- `item_models.py::_human_size()`
- `widgets.py::human_size()`

实现完全相同。

建议移到一个很小的 formatting helper。

---

## L-02：Pyflakes 两处明确无用的异常变量

当前静态检查只报两处：

```text
database.py:1967  local variable 'exc' is assigned to but never used
storage.py:1186   local variable 'exc' is assigned to but never used
```

第一处直接删除 `as exc`。

第二处如果希望保留根因，建议：

```python
except (...) as exc:
    raise RuntimeError(...) from exc
```

而不是捕获后丢失 cause。

---

## L-03：`import_file()` 图片异常分支有完全重复的 strict 判断

当前：

```python
except (OSError, ValueError):
    if strict:
        raise ValueError(...)
    raise ValueError(...)
```

两个分支行为完全一样。

真正决定 strict/non-strict 的是外层 catch，所以这里的 `if strict` 是纯冗余。

---

## L-04：`preflight_current_file()` 对同一文件重复 stat

先：

```python
stat = resolved.stat()
```

随后：

```python
resolved.is_file()
```

`is_file()` 会再次做 filesystem stat，而且第二次检查与第一份 snapshot 之间还引入一个小 TOCTOU 窗口。

建议直接：

```python
stat.S_ISREG(first_stat.st_mode)
```

---

## L-05：`app.main()`、`MainWindow`、`widgets.py` 已经达到“修改成本型”体积

主要体积：

- `widgets.py`：4543 行
- `main_window.py`：3753 行
- `database.py`：2601 行
- `services.py`：2081 行

这里不建议为了“文件短”盲目拆文件。

真正应该拆的是已经形成独立生命周期的模块：

- shutdown coordinator；
- async image AI operation；
- import coordinator；
- library query/search service；
- thumbnail subsystem；
- Win32 frame/backdrop host。

按生命周期拆，而不是按“每 500 行一个文件”拆。

---

## 7. 安全加固建议（非当前正确性 bug）

## S-01：AI API key 以明文保存在本地 settings JSON

目前这是产品明确行为，不应写成漏洞。

但如果用户使用付费 API key，Windows 下可以考虑：

- Credential Manager；
- DPAPI；
- 或至少把 key 与普通 UI settings 分离。

---

## S-02：远程 HTTP endpoint + API key 应提示明文传输风险

`AIService` 支持用户自定义 Base URL。

本地 `http://127.0.0.1` 很合理，但如果：

- scheme 是 HTTP；
- host 不是 loopback；
- 同时设置 API key；

建议至少警告，最好默认拒绝发送 Authorization，避免用户误把密钥通过明文 HTTP 发到远端。

现有 cross-origin redirect 不泄漏 Authorization 的防护值得保留。

---

## 8. 本轮确认“不应乱动”的部分

以下区域虽然代码偏复杂，但当前复杂度有明确安全/平台原因，不应因为第二轮“精简”目标直接砍掉：

### 8.1 Windows managed-file identity 防护

`storage.py` 中：

- reparse point；
- hardlink；
- final path；
- handle identity；
- delete-by-opened-handle；
- migration copy/verify/delete

这些逻辑确实长，但不是无意义堆砌。

### 8.2 Win10 Acrylic helper HWND 几何同步

`windows_frame.py` 的：

- WM_NCCALCSIZE；
- WM_GETMINMAXINFO；
- maximize work area；
- helper HWND SetWindowPos；
- DPI / native maximize

与之前真实视觉/resize 问题直接相关，应继续依靠 visual + interactive gate 修改。

### 8.3 NativeAOT Acrylic bridge的 detach cleanup

C# bridge 在 attach failure、detach、host backdrop state rollback 上的防御性代码有必要。

目前没有发现值得为了“少几行”删除的核心资源清理。

### 8.4 数据库 schema / backup 校验

schema validation 和 backup/restore 代码长，但大部分是在处理损坏恢复、版本迁移和不可信本地文件。

这里的主要优化点是减少重复 I/O，不是削弱校验。

---

## 9. 建议修复顺序

### 第一批：风险低、收益明显

1. 修 release notes 的 Acrylic 硬编码；
2. 修 interactive verifier 必须验证 backend；
3. 调整 interactive performance gate 的 outlier 判据；
4. 修 smoke-ready 80 次后的 terminal failure；
5. `_refresh_navigation_metadata()` collections 只查一次；
6. 删除 `get_item()` 无用 outer tag joins；
7. 清理两处 pyflakes、strict 重复分支、重复 stat；
8. 合并 human-size helper。

这批大多是局部改动，可单独验证。

### 第二批：性能优化

9. 重写 `mark_missing_files()` 为单次 identity-locked verification；
10. 批处理增加 count / ID-only DB API；
11. 重构 import result，顺带去掉 UI 预 hash / 二次查 ID；
12. 减少 clipboard image save 的重复 hash/decode；
13. 对大库搜索引入后台查询；
14. 再决定是否上 FTS5。

### 第三批：结构重构

15. 合并 AI/OCR orchestration；
16. 统一 shutdown state machine；
17. 把跨模块私有能力提升为明确 internal API；
18. 最后再拆 `main_window.py/services.py/widgets.py` 的自然子系统。

---

## 10. 不建议的修法

### 不要因为函数长就拆

`import_file()`、shutdown、Acrylic 的问题不是“函数行数”，而是：

- 权威状态重复生成；
- 生命周期重复实现；
- 多层都做同一种校验。

如果只是机械地把 249 行拆成 8 个 helper，总 I/O 和状态复杂度不会下降。

### 不要为了性能删除最终身份校验

导入/删除/迁移中的很多二次验证是为了 TOCTOU 和 reparse/hardlink 安全。

正确优化方式是：

> **让同一个 verified snapshot 在层之间传递，而不是删掉安全检查。**

### 不要现在大改 Acrylic 实现

当前 Win10 Acrylic 已经有真实视觉 smoke 和交互测试基础。

先修 gate 本身，再做任何 backdrop 重构。

---

## 11. 第二轮最终结论

第一轮之后，ClipSave 已经不是“到处都有明显死代码”的状态了。

第二轮暴露出的核心矛盾变成了：

> **为了安全与兼容性增加的保护层，开始在多个层级重复执行；为了处理异步与退出场景增加的独立流程，开始形成重复状态机。**

因此下一阶段最有价值的“精简”不是继续搜索单个没引用的函数，而是减少：

- 同一个文件被重复 hash；
- 同一张图被重复 decode；
- 同一个数据库状态被重复 query；
- 同一种 AI 请求被写两套；
- 同一个 shutdown 生命周期被维护两套；
- 同一个验证目标由多个脚本各自猜状态。

如果按本报告的顺序处理，代码行数会继续下降，但更重要的是：

- I/O 会真正减少；
- UI 大库性能会真正提升；
- 退出/异步状态更容易证明正确；
- Acrylic release gate 会更可信；
- 未来第三轮审计的误删风险也会显著降低。

---

## 12. 建议作为下一轮修改的验收门槛

完成第二轮修复后至少要求：

1. 全量 unittest 继续 532+ 全绿；
2. compileall PASS；
3. diff check PASS；
4. NativeAOT backdrop build PASS；
5. visual smoke PASS；
6. 修正后的 interactive gate PASS；
7. 新增大库 query benchmark / 性能回归测试；
8. import/reconcile 使用 mock/计数器证明完整 hash 次数下降；
9. session-end timeout 测试证明没有游离 writer 改变当前生命周期；
10. release workflow 生成的版本主题只来自当前 CHANGELOG，不再硬编码旧版本主题。

---

## 13. 第二轮实施结果

本报告完成后已经按风险顺序实施主要修复。最终状态如下：

- 搜索输入、扩大搜索、导航、排序、分页，以及正常 mutation 后的资料库刷新已经移出 GUI 主线程；所有异步列表查询都有 request/token 与 stale-result 防护。
- `mark_missing_files()` 的正常文件校验由两次完整 SHA-256 收敛为一次 identity-locked hash。
- 外部图片导入不再由 UI 预先 decode/hash，也不再通过 hash 反查刚导入的 ID；数据库提供结构化 `ImportFileDetails`，并减少一次重复 target hash 与一次重复图片打开。
- Clipboard 图片保存复用已经验证的 PNG hash、size 和 QImage dimensions，数据库不再重新 hash/decode 同一张刚保存的图片。
- AI/OCR 请求编排已合并；结果保存始终要求 expected content hash 并走 compare-and-set。
- AI HTTP response body 的阻塞读取现在受整体 wall-clock deadline 约束。
- session-end note writer 已纳入可追踪 async task；正常退出与 session-end 共享后台请求取消/清理和最终 shutdown tail。
- smoke ready/status、Acrylic backend 验证、性能统计规则和 release checklist 已补齐。
- release notes 不再硬编码上一个 Acrylic 版本主题。
- `get_item()` 的无效 outer tag joins / `GROUP BY` 已删除；列表页双 tag correlated subquery 暂时保留，等待独立 benchmark 后再决定结构。
- FTS5 暂未引入；当前先通过后台查询消除了最直接的 GUI 卡顿风险，避免在 1.1.2 同时引入 schema/search 迁移。

### 13.1 Acrylic gate 中途失败的真正原因

第二轮实施后曾连续观察到 resize p95 约 20–21 ms，但：

- backend 始终为 `win10_effect_acrylic`；
- helper HWND geometry delta 始终为 0 px；
- source 与 frozen EXE 都出现相同现象；
- 本轮没有改动 native resize/backdrop handler。

最终确认不是 Acrylic 性能回归，而是 **smoke ready 过早**：

1. startup scan 完成后，`_startup_scan_request` 先被清空；
2. completion callback 随即启动新的异步 library refresh；
3. 原 smoke 只等待 `_startup_scan_request is None` 就发布 ready；
4. interactive gate 因此与仍在执行的 SQLite/library refresh 并发进行 resize 测量。

修复后，smoke ready 必须同时等待 startup scan、library refresh、search query 与 page query 全部静止。无需放宽性能阈值，原统计 gate 即恢复稳定通过。

### 13.2 最终验收

最终工作树验证结果：

- unittest：**554 / 554 PASS**
- Pyflakes：**0 warnings**
- `python -m compileall`：PASS
- `git diff --check`：PASS
- NativeAOT Windows backdrop build：PASS
- `build.bat`：PASS
- portable package：`ClipSave-1.1.2-UNOFFICIAL-windows-x64.zip`
- visual smoke：PASS，backend=`win10_effect_acrylic`
- interactive backdrop：
  - move p95 = **7.182 ms**
  - resize p95 = **9.683 ms**
  - resize p99 = **15.808 ms**
  - resize >16.7 ms = **1 / 240**
  - resize >33.3 ms = **0 / 240**
  - move/resize geometry max delta = **0 px**
  - result = **PASS**

本机未安装 Inno Setup，因此本地构建只生成 portable ZIP，没有生成 installer；这不影响上述应用、NativeAOT、visual 或 interactive 验证。
