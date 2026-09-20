# ClipSave 1.1.2 第三轮完整实现审计报告

日期：2026-09-20  
审计基线：`main @ c2a75b512a5c71184c6787d566e55f1f83ccf27a`  

> 本轮是第二轮大规模修复后的实现边界审计。重点不再是继续机械拆文件，也不以普通死代码为主要目标，而是检查：异步取消是否真的减少工作、任务状态机是否可回滚、bulk/manual/automatic 是否共享资源预算、凭据是否真正删除、兼容数据是否仍能走完整功能链，以及测试全绿仍可能遗漏的恢复/异常路径。

---

## 0. 第三轮修复完成状态（审计后实施）

本报告最初记录的是 `c2a75b5...` 基线上的确认问题。随后已按 Phase A → B → C 完成实现修复；本节记录修复后的实际状态，后文原始审计证据保留不删，便于追溯问题来源。

### 0.1 已完成的核心修复

- **H-01 Library stale query backlog：已修复。**
  - `LibraryController` 不再为每个替换请求创建一个独立查询线程；
  - 改为短生命周期单 worker 串行 drain；
  - 同一种 pending work 永远只保留最新一个；
  - 实证回归由原来的 `q0,q1,q2,q3,q4` 全部执行，收敛为只执行 `q0` 与最终 `q4`；
  - 不引入常驻线程，因此原有 shutdown / smoke idle 语义保持不变。

- **H-02 bulk / manual / automatic AI-OCR 调度割裂：已修复。**
  - 新增 `ImageWorkCoordinator`；
  - `(item_id, operation)` 现在跨 manual / automatic / bulk 共享唯一任务身份；
  - bulk OCR/description 也通过现有 `BoundedTaskExecutor`；
  - decoded image memory 进入同一个 512 MiB 全局预算；
  - claim 生命周期覆盖 provider 调用直到 UI/CAS commit，取消、late result、force-clear 均有释放路径。

- **H-03 external image AI/OCR：已修复。**
  - managed image 仍使用 ClipSave picture root；
  - external indexed image 显式使用其父目录作为 identity-checked local root；
  - 没有退回裸 `Path.open()`，Windows identity / reparse / hard-link 防护继续保留。

- **M-01 API Key backup secret：已修复。**
  - `settings.json.bak` 不再保存 `ai_api_key`；
  - 清空 key 后 primary 与 backup 均不再残留旧 secret；
  - primary 损坏、仅旧 backup 可读时，后续保存也会清理 legacy secret。

- **M-02 task start rollback：已修复。**
  - `TaskSupervisor.start_thread()` 在 `thread.start()` 失败时事务化撤销 token；
  - Library / Maintenance controller 同步回滚 request 状态；
  - delete start failure 不再先乐观隐藏 UI；
  - GUI 层有直接回归测试保证“任务没启动 => item 仍可见”。

- **M-03 bulk checkpoint 跨持久化域重复付费：已修复。**
  - checkpoint 升级到 **v2**；
  - provider 返回后，先持久化 `pending_stage + pending_text + pending_content_hash`，再写 SQLite；
  - DB 写成功但后续 checkpoint 推进失败时，Resume 重放本地结果，不再重复调用对应 provider；
  - pending result 与 `content_hash` 绑定，图片被替换时不会把旧结果写到新内容；
  - 旧 v1 checkpoint 可兼容读取并升级为 v2。

- **M-04 非 loopback HTTP + API Key：已加固。**
  - `http://localhost`、`127.0.0.1`、`::1` 继续允许携带 API Key，兼容本地 OpenAI-compatible 服务；
  - 非 loopback 明文 HTTP + API Key 会在任何网络请求前直接拒绝，并要求使用 HTTPS。

- **M-05 executor shutdown / database close 顺序：已修复。**
  - `ApplicationRuntime.close_core()` 现在返回 `bool`；
  - executor 在预算内没有停止时，数据库保持打开，不再 silent-close；
  - `ShutdownCoordinator.finalize_core()` 同步遵守该结果；
  - 主入口仍可在窗口事件循环结束后用完整 timeout 再次关闭 runtime。

### 0.2 修复后最终验证

最终权威回归（所有最后补丁之后重新执行）：

```text
Ran 677 tests in 188.566s

OK
```

即：**677 / 677 PASS，0 failure，0 error**。

其他验证：

- `python -m pyflakes clipsave_app tests`：PASS
- `python -m compileall -q clipsave_app tests`：PASS
- `git diff --check`：PASS
- 内部模块依赖图：**80 modules / 184 edges / 0 cycles**
- Windows backdrop NativeAOT build：PASS
- PyInstaller onedir build：PASS
- release manifest / ZIP：PASS
- Inno Setup：本机仍未安装，因此没有 installer；与此前环境一致，不是回归

本轮 dirty-tree 验证产物：

```text
build\release\ClipSave\ClipSave.exe
SHA-256 2C8D09F7A108ECF21BDC741D692CD5B56216DFC62EE95B879F4CD2AB62C9E856

build\release\ClipSave-1.1.2-UNOFFICIAL-windows-x64.zip
SHA-256 A078BD2472B52CDEBF9115FCA7A0319CD02330EFF4A153A550975D2424EAA7BB
```

由于修复尚未提交，`BUILD_INFO.txt` 正确标记为：

```text
Commit c2a75b512a5c71184c6787d566e55f1f83ccf27a-dirty
```

因此这两个产物只用于本轮验证，不应作为最终官方发布产物。正式 release 应在 commit 后重新 clean-build。

---

## 1. 最终结论

### 1.1 没有发现 P0 级数据破坏问题

本轮没有发现新的“正常使用会稳定损坏数据库、误删 managed 文件、覆盖错误 backup、破坏 Win10 Acrylic 主路径”之类灾难性问题。

第二轮以后，下列底层仍然可靠：

- managed path / reparse / hardlink / HANDLE identity 防护；
- delete/recycle 前 hash + size 校验；
- import staging 的 identity-locked source；
- SQLite transaction / backup / recovery；
- legacy storage migration 的 copy → verify → DB rebind → delete 顺序；
- SafeMarkdownBrowser 资源隔离；
- SingleInstance 连接限流与超时；
- Win10 Acrylic helper HWND + live move/resize 同步。

### 1.2 第三轮真正值得修的是“跨控制器边界”

最重要的发现有六类：

1. **Library 搜索只取消结果，不取消实际 SQLite 工作。**
2. **bulk / manual / automatic AI-OCR 没有统一任务身份、去重和内存预算。**
3. **数据库支持 external image，但 AI/OCR 强制所有图片位于 picture root。**
4. **用户清空 API Key 后，旧 secret 仍保留在 `settings.json.bak`。**
5. **线程启动失败时，部分 request / UI 状态回滚不是事务化的。**
6. **bulk checkpoint 与数据库结果写入跨两个持久化域，失败窗口会重复昂贵请求。**

这说明下一步架构优化不应该继续以“拆更多文件”为目标，而应该统一：

> task identity、cancel semantics、resource budget、persistence boundary。

---

## 2. 审计规模与验证基线

当前源码规模：

- `clipsave_app/*.py`：**79 个模块**
- Python 应用代码：**21,869 行**
- `tests/test_*.py`：**48 个测试文件**
- 内部模块依赖图：**178 条边，0 个循环依赖**

静态检查：

- `python -m pyflakes clipsave_app tests`：PASS
- `python -m compileall -q clipsave_app tests`：PASS
- `git diff --check`：PASS（审计基线）

最终完整回归：

```text
Ran 651 tests in 196.539s

OK
```

即：**651 / 651 PASS**。

因此下面的确认缺陷，都是“测试全绿仍没覆盖”的边界问题。

---

## 3. 本轮三个关键实证

### 3.1 搜索替换不等于工作取消

连续发出 5 个彼此替换的搜索请求，并让底层查询阻塞，结果：

```text
regular_tasks= 5
current_request= 4
```

UI 当前 request 只有最后一个，但旧工作线程全部仍在。

### 3.2 external image 无法进入 AI/OCR

把 `picture_root` 设为 managed Pictures，再给 `AIService._encode_image()` 一个 picture root 外的合法图片，实际得到：

```text
RuntimeError: Managed file is outside its local root: ...\external.png
```

### 3.3 清空 API Key 并没有清掉 backup 中的旧 key

先保存：

```text
ai_api_key = SECRET_OLD_KEY
```

再设为空，结果：

```text
primary=
backup= SECRET_OLD_KEY
```

---

# 4. 高优先级发现

## H-01：Library 搜索是 latest-result-wins，但不是 latest-work-wins

### 位置

- `library_controller.py`
- `task_supervisor.py`
- `database_query_store.py`

### 当前行为

新搜索会调用：

```python
self.cancel_search()
```

最终只是：

```python
self.supervisor.cancel(request.token)
```

即给旧线程的 `cancel_event` 置位。

但是一旦旧线程已经进入：

```python
items = self.query_items(...)
```

SQLite 查询本身不会检查 `cancel_event`。

查询结束后只是通过：

```python
if not cancel_event.is_set():
    emit(...)
```

丢弃旧结果。

真实语义 therefore 是：

> 旧结果不会覆盖新结果，但旧计算会完整执行。

### 影响

`LibraryDatabase` 仍共享同一个 SQLite connection，并用锁保护访问，所以 stale query 不只是浪费 CPU：

1. 会继续做 `%LIKE%` 扫描；
2. 多个旧查询可能排队；
3. 新查询也要等；
4. metadata mutation / notes / delete reconcile 等 DB 操作也可能被拖慢；
5. 大库下后台 backlog 会随着快速搜索输入增长。

第二轮把重查询移出 GUI 线程是正确的，但第三轮暴露出新的问题：

> UI 不再直接卡住，过期工作却可能在后台累积。

### 推荐修法

推荐建立单一 Library Query Worker：

- 同一时间只执行一个 query；
- 新 request 到来时只替换 pending；
- pending 永远只保留最新请求；
- 当前 query 结束后跳到最新 pending；
- 中间 stale request 永远不进入 SQLite。

如果未来要真正中断正在执行的 SQL，再考虑独立 read connection + progress handler / interrupt。

### 必补测试

- 快速替换 10 次搜索，底层不应执行 10 次完整 query；
- stale query 不应线性增加 shutdown 等待；
- stale query 不应阻塞后续 metadata write。

---

## H-02：bulk / manual / automatic AI-OCR 没有共享任务身份与资源预算

### 位置

- `image_task_controller.py`
- `bulk_image_controller.py`
- `bulk_image_job.py`
- `task_executor.py`

### 单张任务目前是受控的

手动/自动单张 AI-OCR 会进入：

```python
BoundedTaskExecutor(
    max_active=2,
    max_queued=4,
    memory_budget_bytes=512 * 1024 * 1024,
)
```

并按：

```python
width * height * 4
```

预留 decoded image memory。

### bulk 绕过整个 bounded executor

`BulkImageController` 使用普通 regular thread，`BulkImageJob` 直接调用：

```python
preflight_image_file(...)
service.ocr_image(...)
service.describe_image(...)
```

因此 bulk：

- 不占 bounded slot；
- 不占 512 MiB 全局预算；
- 不登记在 `ai_requests / ocr_requests`；
- manual / automatic 不知道 bulk 正在处理哪个 item。

### 后果 A：同一图片可重复调用 provider

bulk 正在 OCR item X 时，manual OCR item X 仍可发起；automatic 也没有统一的 cross-controller active key。

最后两边都只用：

```text
id + content_hash + missing=0
```

做 CAS。

内容没变时，两边都可以成功提交，造成：

- 重复请求；
- 重复 API 费用；
- 重复算力和网络；
- 最晚完成者覆盖前者。

### 后果 B：512 MiB 不是实际全局 budget

当前最大图片允许 100,000,000 pixels，RGBA 估算约 400 MB。

一个 bulk 大图与一个 manual 大图并行，仅 decoded buffer 就可能接近 800 MB；再加 PIL、JPEG encode、HTTP payload 等，峰值更高。

### 推荐修法

建立统一 ImageWorkCoordinator（名称可不同），至少统一：

- `(item_id, operation)` active key；
- manual / automatic / bulk owner；
- memory reservation；
- bounded slot；
- cancellation；
- commit lifecycle。

bulk 仍可按 checkpoint 一张张处理，但每一个 OCR/description stage 都应通过同一个 coordinator 取得执行权。

---

## H-03：数据库支持 external image，但 AI/OCR 只允许 picture root 内图片

### 已存在的 external 语义

数据库 schema 有：

```text
external
```

`import_file(... copy_to_library=False)` 明确支持外部索引；删除逻辑也专门区分 managed 与 external。

所以 external file 不是无意遗留字段，而是数据模型的一部分。

### AIService 的冲突

`AIService._encode_image()` 固定：

```python
with open_managed_binary(
    snapshot.path,
    "rb",
    self._picture_root(),
    identity_locked=True,
)
```

也就是把所有 image 都当作 picture root 内 managed file。

external indexed image 因而会直接被安全检查拒绝。

影响：

- manual OCR；
- manual description；
- bulk OCR；
- bulk description；
- legacy DB 中保留的 external image。

### 正确修法

不能退回普通 `Path.open()`。

应该继续保留 identity-lock：

- managed image：root = ClipSave picture/library root；
- external image：root = 当前外部文件 parent；
- 最好由 database/controller 产生一个带 ownership/root 的 verified ImageSource，而不是 AIService 自己猜。

### 测试缺口

现有测试分别覆盖了 external indexing 和 AI picture_root，却没有组合成“external indexed image + OCR/description”。

---

# 5. 中优先级发现

## M-01：清空/更换 API Key 后，旧 key 继续保留在 `settings.json.bak`

### 位置

`settings.py -> Settings.save()`

每次发布新 primary 前，会把旧 primary 完整复制成 `.bak`：

```python
current = _read_settings(self.path)
backup_data.update(current)
os.replace(backup_temporary, self.backup_path)
```

由于 settings 里包含 `ai_api_key`，用户清空 key 后：

- primary：已空；
- backup：仍保留旧 secret。

而 primary 损坏时，程序会读取 `.bak`，因此旧 AI endpoint/model/auto flags/key 也可能恢复。

### 推荐修法

最低限度：

- backup 永远不保存 `ai_api_key`；或
- API key 单独迁移到 Windows Credential Manager / DPAPI；
- 用户清空 key 时 primary + backup 中 secret 必须同时消失；
- recovery 不应静默恢复用户已经撤销的 secret。

这不是远程泄露漏洞；问题是 UI 上看起来已经删除的 credential，磁盘上并没有同时删除。

---

## M-02：任务启动不是事务化操作，`thread.start()` 失败会破坏 request/UI 状态

### TaskSupervisor 底层顺序

当前：

```python
self.regular_tasks[token] = (cancel_event, thread)
thread.start()
```

如果 `thread.start()` 因系统资源问题抛异常：

- token 已登记；
- 线程从未运行；
- `run()` finally 不会清掉 token；
- 上层 controller 必须各自正确回滚。

### delete 路径存在明确状态断裂

`LibraryMutationController.start_delete()` 启动失败时先：

```python
self.delete_requests.pop(item_id, None)
self.pending_delete_item_ids.discard(item_id)
self.delete_failed.emit(...)
```

但 MainWindow `_delete_failed()` 第一件事是：

```python
restore = finish_delete(...)
if restore is None:
    return
```

controller 已经 pop，因此 restore snapshot 丢失。

caller 随后做 optimistic UI hide，并检测 request 不存在后再调用 `_delete_failed()`，仍然无法 restore。

极端情况下结果是：

- 文件/索引没删；
- UI 却把 item 乐观隐藏；
- selection/detail 恢复不了；
- failure message 也可能不显示。

### maintenance 也有同类风险

`start_backup()` / `start_scan()` 先记录 request 再启动线程，没有统一 rollback。

尤其 `backup_request` 残留后，`start_backup_if_dirty()` 会一直认为已有 backup 正在运行。

### 推荐修法

`TaskSupervisor.start_thread()` 必须本身事务化：

```text
register
-> start
-> start failure => unregister + re-raise
```

controller 层统一语义：

- 成功：返回有效 request；
- 失败：完整 rollback 并 raise；
- 不要同步 emit failure 后仍返回失效 request。

---

## M-03：bulk checkpoint 与 SQLite result commit 不在同一个事务中

### OCR stage 当前顺序

```text
provider OCR
-> update_ocr_if_current() 写 SQLite
-> checkpoint.at_stage("description")
-> save_checkpoint()
```

如果 DB 成功、checkpoint 保存失败：

- OCR 已在数据库中；
- checkpoint 仍是 `ocr`；
- 下次 Resume 会再次请求 OCR。

description completion 同样存在：

```text
provider describe
-> update_ai_if_current()
-> checkpoint.advance("completed")
-> save_checkpoint()
```

最后一步失败就会重复 description。

这不会损坏数据，但会重复 provider 调用与费用。

### 推荐修法

长期最干净的是把 bulk job progress 放进 SQLite，与 item result 同一 transaction。

若 1.1.2 不想改 schema，就要给 checkpoint 增加足够的 job/stage identity，使 Resume 能区分：

- 用户旧有 OCR/description；
- 当前 bulk job 已经成功写入但 checkpoint 未推进的结果。

现有测试只覆盖“provider description 失败时保留 description stage”，没有覆盖“DB commit 成功后 checkpoint 写失败”。

---

## M-04：非 loopback `http://` + API Key 会明文发送 Bearer credential

当前本地 OpenAI-compatible 服务需要支持：

```text
http://127.0.0.1:...
http://localhost:...
```

所以不能全面禁止 HTTP。

但当前没有区分：

- loopback HTTP；
- LAN HTTP；
- public HTTP。

只要配置了 key，都会发送：

```text
Authorization: Bearer ...
```

### 推荐策略

- loopback HTTP：允许；
- 非 loopback HTTP + 无 key：允许但可提示；
- 非 loopback HTTP + key：默认拒绝或强警告二次确认；
- HTTPS：正常允许。

这是安全加固项，不是本地 AI 兼容性 bug。

---

## M-05：`ApplicationRuntime.close_core()` 忽略 executor shutdown 失败结果

当前：

```python
shutdown_ai_ocr_task_executor(timeout=...)
self.database.close()
self._core_closed = True
```

`shutdown_ai_ocr_task_executor()` 明明返回 `bool`，但结果被忽略。

正常 Quit 路径前面已经等待 tracked bounded tasks，所以通常安全。

但异常 Qt quit、未跟踪 executor task、或 shutdown timeout 情况下，理论上可能出现：

> worker 仍活着，但它依赖的 database 已经被关闭。

推荐让 `close_core()` 返回 bool；executor 没停完时不应静默关闭 DB。

这一项优先级低于前四个 M 项，因为正常交互退出已有多层 gate。

---

# 6. 低优先级 / 可维护性判断

## L-01：`LibraryDatabase.add_image()` 已基本属于 legacy/compatibility API

当前生产 clipboard image 使用 `add_verified_image()`；GUI import 使用 `import_file()`。

本轮没有找到生产代码调用 `database.add_image()`，但测试仍直接覆盖它。

因此它不是“马上可删的死代码”，而是一个兼容入口。

建议：先标注 legacy/internal，未来有明确 API 收口计划再删，不要为了 1.1.2 少几十行强删。

---

## L-02：`services.py` 仍是 compatibility facade，但暂时不值得继续拆

它仍保留 AIService / ClipboardService / backdrop 等历史 patch seam。

从纯代码量角度还能缩，但目前大量测试仍依赖这些 seam，而且真正业务状态已经迁出。

第三轮判断：继续重写 import surface 的回归风险大于收益。

---

## L-03：`MainWindow` 仍很大，但“行数”已经不是继续拆的充分理由

第二轮已经迁出：

- LibraryController
- LibraryMetadataController
- MutationController
- ImageTaskController
- BulkImageController
- MaintenanceController
- MonitoringController
- ShutdownCoordinator
- WindowEffectsController
- NativeWindowController

剩余很多代码属于 Qt composition、signal wiring 和 UI policy。

第三轮没有发现一个“再拆出去就能立刻消除大量 bug”的新巨大边界。

以后如果继续拆，应以真实共享状态/责任为依据，而不是以文件长度为 KPI。

---

# 7. 已重点复核但没有发现新问题的区域

## 7.1 Managed delete / recycle

保留了 managed-root、HANDLE identity、reparse、hardlink、expected hash、expected size、recycle staging 和 DB reconcile。

没有发现新误删路径。

## 7.2 Import staging

`prepare_import()` 仍使用 identity-locked source，copy 后重新校验 hash/size，并让 final identity handle 跨越 DB commit。

没有发现第二轮后新的 TOCTOU 回退。

## 7.3 Database backup / recovery

复核了 quick-check、schema validation、backup identity、WAL/SHM、corrupt preserve、restore publication、untrusted quarantine、backup generation。

没有发现新的数据破坏缺陷。

## 7.4 Legacy storage migration

当前仍是：

```text
copy
-> verify
-> rebind DB
-> delete legacy source
```

顺序正确，不建议重写。

## 7.5 Markdown preview

`SafeMarkdownBrowser` 仍拒绝 relative/local/non-qrc resource，没有重新开放网络或本地文件资源。

## 7.6 SingleInstance

per-user endpoint、mutex、connection cap、client timeout、ACK、stale endpoint 处理仍合理。

## 7.7 Win10 Acrylic / native window

复核 helper HWND、`WM_WINDOWPOSCHANGING` 同步、interactive resize、maximize work-area、power/transparency policy、Win11/Win10 backend 切换。

没有发现第二轮后的新架构级问题。

这一块现在应以保持稳定为主。

---

# 8. 推荐修复顺序

## Phase A：先修明确行为错误

1. **external image AI/OCR root 语义**
2. **TaskSupervisor/controller 启动事务化**
3. **settings secret backup 生命周期**

这三项边界相对独立，适合先落地。

## Phase B：统一异步任务模型

4. **Library query single-worker latest-work-wins**
5. **bulk AI/OCR 接入统一 ImageWorkCoordinator / bounded executor**

这是第三轮最有架构价值的两项。

## Phase C：恢复语义与防御性退出

6. bulk checkpoint 幂等恢复
7. `runtime.close_core()` 尊重 executor shutdown 结果
8. 非 loopback HTTP + API key 安全策略

---

# 9. 建议新增回归测试

至少新增：

1. `test_replaced_library_search_does_not_execute_all_stale_queries`
2. `test_external_indexed_image_can_run_ocr`
3. `test_external_indexed_image_can_run_description`
4. `test_task_supervisor_rolls_back_token_when_thread_start_fails`
5. `test_delete_start_failure_restores_optimistic_ui_state`
6. `test_backup_request_rolls_back_when_thread_start_fails`
7. `test_clearing_api_key_removes_secret_from_backup`
8. `test_bulk_and_manual_same_operation_are_deduplicated`
9. `test_bulk_ai_memory_counts_toward_global_budget`
10. `test_bulk_ocr_db_commit_then_checkpoint_failure_is_idempotent`
11. `test_bulk_description_db_commit_then_checkpoint_failure_is_idempotent`
12. `test_remote_plain_http_with_api_key_is_rejected_or_confirmed`
13. `test_runtime_does_not_close_database_while_executor_shutdown_times_out`

---

# 10. 第三轮架构裁决

第二轮之前，主要问题是：

- GUI 同步大查询；
- 重复 hash/decode；
- shutdown 双状态机；
- controller 边界不清；
- Acrylic validation / lifecycle；
- sqlite.Row 等底层结构直接泄漏到 UI。

这些已经显著改善。

第三轮看到的是下一层：

> controller 已经拆出来，但 controller 之间还没有统一“任务身份、取消语义、资源预算和持久化事务边界”。

所以如果还要做一轮“大刀架构优化”，下一刀应该是：

1. 统一 async work lifecycle；
2. 统一 image AI/OCR scheduling；
3. 让 cancel 真正减少 work，而不是只丢弃 result；
4. 把 sensitive settings 与普通 UI settings 分开。

这四项的价值远高于把 300 行文件再拆成两个 150 行文件。

---

# 11. 最终停止准则

当前 1.1.2 已经不属于“代码质量很差，需要继续大规模重写”的状态。

第三轮仍找到了一批真实问题，但精简收益已经明显进入递减区间。

后续继续删除/重构应服从一个标准：

> 必须能消除一个真实状态、一个重复责任、一个竞态面，或一个可量化资源成本；仅仅为了少几十行，不再值得承担回归风险。

这应该成为 ClipSave 1.1.2 后续代码精简工作的停止准则。

---

# 12. 第三轮修复实施状态（2026-09-20）

本报告列出的高/中优先级问题现已完成一轮实现修复，并通过完整回归。

## H-01：Library stale query backlog — 已修复

`LibraryController` 改为单一 `ClipSaveLibraryQuery` worker：

- 同一时间只执行一个 Library 查询；
- refresh/search/page 每类最多保留一个 pending；
- 同类新请求直接覆盖旧 pending；
- 已经执行中的 stale query 结果会被丢弃，但不会再继续启动多个 stale worker；
- 实测连续 q0→q4 快速替换时，底层只执行 `q0` 和最新 `q4`，而不是 5 个查询。

## H-02：bulk/manual/automatic AI-OCR 调度分裂 — 已修复

新增 `ImageWorkCoordinator`：

- `(item_id, operation)` 作为统一任务身份；
- manual / automatic / bulk 共享同一个 coordinator；
- 相同 OCR/AI operation 不再重复调用 provider；
- bulk 每个 OCR/description stage 也进入全局 `BoundedTaskExecutor`；
- bulk 因此正式计入原有 512 MiB AI/OCR 内存预算；
- bulk 等待 busy/capacity 时保持可取消，不占用额外 executor memory reservation。

## H-03：external indexed image 无法 AI/OCR — 已修复

AIService 新增显式 `source_root`：

- managed image 仍使用 database picture root；
- external image 使用其已索引文件的 parent 作为 identity root；
- 两者都继续走 `open_managed_binary(... identity_locked=True)`；
- 没有退回不安全的普通 `Path.open()`。

## M-01：API Key 残留在 settings backup — 已修复

`settings.json.bak` 不再保存 `ai_api_key`：

- 清空 key 后 primary 与 backup 都为空；
- 更换 key 时旧 key 不会进入 backup；
- 从 legacy backup 恢复后再次保存/清空时也会 scrub secret。

## M-02：thread.start() 失败破坏 controller 状态 — 已修复

- `TaskSupervisor.start_thread()` 现在在 `thread.start()` 失败时自动 unregister token；
- Library / Maintenance controller 的 request 会回到 idle；
- delete worker 启动失败改为 rollback + raise；
- MainWindow 只有真正启动成功后才做 optimistic delete UI hide；
- 删除启动失败会保留条目并向用户显示错误。

## M-03：bulk DB commit 与 checkpoint 不原子 — 已修复为幂等恢复

bulk checkpoint 升级到 v2，加入：

- `pending_stage`
- `pending_text`
- `pending_content_hash`

provider 返回结果后先安全落 checkpoint，再写数据库。

如果数据库写成功、后续 checkpoint stage/advance 保存失败，下次恢复会复用已经持久化的 provider result，而不会再次调用 OCR/AI provider。

v1 checkpoint 可兼容加载并自动升级为 v2 语义。

## M-04：remote HTTP + API Key 明文 Bearer — 已修复

AIService 现在执行 transport validation：

- `http://localhost`
- `http://*.localhost`
- `http://127.0.0.1`
- `http://::1`

仍允许本机开发/LM Studio/Ollama 类服务使用 HTTP + API Key。

非 loopback 的明文 HTTP 如果配置 API Key，会在任何网络请求发生前直接拒绝，并要求 HTTPS。

## M-05：executor shutdown 失败仍关闭 DB/退出 — 已修复整条退出链

- `ApplicationRuntime.close_core()` 现在返回 bool；
- executor 未真正停止时，不关闭 database；
- `ShutdownCoordinator.finalize_core()` 在关闭 thumbnail/database 前先验证 compute executor 已停止；
- normal quit 与 session-end quit 都在不可逆 clipboard/thumbnail shutdown 之前增加 compute-executor gate；
- gate 失败时恢复 UI/monitoring 状态，不继续关闭应用；
- `MainWindow._finalize_shutdown()` 也不再无视 `finalize_core()` 的失败结果。

## 最终验证

第三轮修复后的完整结果：

```text
Ran 681 tests in 193.080s

OK
```

即：**681 / 681 PASS**。

相较第三轮审计基线的 651 个测试，本轮新增 **30 个回归测试**。

同时：

- `python -m pyflakes clipsave_app tests`：PASS
- `python -m compileall -q clipsave_app tests`：PASS
- `git diff --check`：PASS

截至本次修复结束，第三轮报告中的 H-01 ~ H-03、M-01 ~ M-05 均已有对应实现和回归覆盖。

