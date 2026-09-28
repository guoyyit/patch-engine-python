# patch-engine

本地补丁应用与冲突消解服务。接收 **unified diff** 补丁，对多个文本文件执行修改；
支持批次原子提交、乐观并发控制、确定性模糊匹配、崩溃恢复，以及面向大文件的
有界内存流式处理。

- 语言/运行时：Python 3.14，FastAPI（仅 HTTP 层）
- 持久化：内容寻址 blob + 原子 state 指针 + append-only WAL（JSONL）
- 无外部数据库；所有状态保存在一个本地目录中

---

## 1. 目录结构与模块职责

| 模块 | 职责 |
| --- | --- |
| `patch_engine/parser.py` | 解析 unified diff：文件头、`@@` 行范围、上下文/增/删行、`\ No newline` 标记 |
| `patch_engine/matcher.py` | 单个 hunk 的定位：精确位置 → 最近精确匹配 → 模糊匹配；越界/上下文不匹配的可区分拒绝 |
| `patch_engine/applier.py` | 把一个 `FilePatch` 应用到行列表；多 hunk 组合、重叠检测、整文件/逐 hunk 幂等 |
| `patch_engine/stream.py` | 大文件两遍流式处理（有界滚动窗口 + 块拷贝），内存与活动 hunk 相关 |
| `patch_engine/storage.py` | blob 存储、工作副本、原子 `state.json` 指针、路径逃逸防护 |
| `patch_engine/wal.py` | fsync 的提交日志（PREPARE / COMMIT），撕裂尾行修复 |
| `patch_engine/service.py` | 事务编排：批次、回滚、并发锁、版本校验、恢复、时限与指标 |
| `patch_engine/api.py` | FastAPI HTTP 接口与错误码映射 |
| `tests/` | 解析、匹配、应用、批次回滚、并发竞争、崩溃恢复、大文件、HTTP 测试 |

---

## 2. 支持的补丁格式

`diff -u` / `git diff` 风格的子集：

```
diff --git a/path b/path        # 可选
new file mode 100644            # 可选，新建
deleted file mode 100644        # 可选，删除
--- old/path [时间戳]           # 必选，/dev/null 表示新建
+++ new/path [时间戳]           # 必选，/dev/null 表示删除
@@ -l,c +l,c @@ [section]       # 一个或多个
 上下文行
-删除行
+新增行
\ No newline at end of file     # 可选
```

- 路径前缀 `a/`、`b/` 会被剥离；`---`/`+++` 后的 tab 时间戳被忽略。
- **行范围计数按 hunk 头严格校验**。正文行数与 `-l,c +l,c` 不一致即
  `malformed_hunk`。计数也用于消除歧义：例如删除行恰好以 `---` 开头时，
  解析器按计数消费正文，不会把它误判成下一个文件头。
- 一个补丁可含多个文件段，一个文件段可含多个 hunk。

### 解析期拒绝原因（HTTP 422，互不相同）

| `reason` | 含义 |
| --- | --- |
| `empty_patch` | 空文本 / 纯空白 / 找不到任何合法文件段 |
| `malformed_header` | 缺 `---`/`+++`、hunk 出现在文件头之前、裸 `+++` 等 |
| `malformed_hunk` | hunk 头无法识别、正文计数与声明不符、非法正文行、非法 `\` 标记 |

---

## 3. 应用语义与冲突判定规则

对每个 hunk，按**固定顺序**定位（全部确定性，无随机选择）：

1. **精确声明位置**：旧侧内容（上下文 + 删除行）出现在 hunk 头声明的位置 →
   `offset=0, fuzz=0`。
2. **最近精确匹配**：完整 pre-image 在文件别处出现时，选择
   `|实际位置 - 声明位置|` 最小者；距离相同取**行号更小**者。记录 `offset`。
3. **模糊匹配（fuzz）**：仿照 GNU patch，最多丢弃 `max_fuzz`（默认 2）个
   **首尾上下文行**；优先更小的 fuzz，同级仍按"最近、行号更小"裁决。
   - 被丢弃的上下文行不参与匹配，但仍按原样保留在输出中。
   - 模糊不得抹掉全部实际变更（pre/post 完全相同的退化匹配被拒绝）。

### 应用期拒绝原因（HTTP 409）

| `reason` | 触发条件 |
| --- | --- |
| `line_range_out_of_bounds` | hunk 声明的旧侧范围（含 `-,0` 插入点）落在文件之外。**此时不做任何位置猜测**，直接拒绝 |
| `context_mismatch` | 范围合法，但精确与（允许的）模糊匹配都找不到 pre-image；或多个 hunk 定位后区间重叠 |

两者刻意区分：**行号越界**与**位置对但内容对不上**是不同的失败原因，
调用方可据此决定"补丁过期"还是"文件结构变化"。

### 绝不使用错误位置继续应用

- 定位与校验全部发生在任何写入之前；任何 hunk 失败，整批丢弃内存结果，
  磁盘上不留半应用状态。
- hunk 区间重叠会被确定性拒绝，避免歧义合成。

### 幂等性

- 同一补丁重复应用：若 post-image 已存在（先查声明新位置，pre-image 定位失败时
  在其附近窗口查找 post-image），该 hunk 视为已应用而跳过；整个补丁无内容变化时
  **不产生新版本**。
- 重放**同一批次 id**（`batch_id`）返回成功且不会二次应用。

### 顺序独立性

同一批次内或不同批次中，**针对不同文件、相互独立**的补丁，无论提交顺序如何，
最终内容一致（见 `test_independent_patches_order_invariant`）。针对同一文件的
补丁按其在批次中的出现顺序链式应用。

---

## 4. 批次与原子性

`POST /batches` 接收 `diffs: [...]`，作为**一个事务**：

1. 先解析/校验所有 diff（任一畸形 → 整体拒绝，无锁无 IO）；
2. 按路径**排序加锁**（避免死锁），加载最新 state，校验 base 版本；
3. 在内存（或流式临时文件）中计算全部新内容——失败即丢弃，无持久化；
4. 写新 blob；
5. `WAL.append(PREPARE, 完整新状态清单)` 并 fsync；
6. 更新工作副本、原子发布 `state.json`（temp + `os.replace`）；
7. `WAL.append(COMMIT)` 并 fsync。

任一补丁失败 → 整个批次回滚到提交前：文件内容、版本、state 指针均不变，
WAL 中也不会留下该批次记录。

**一个批次对同一个文件只产生一个新版本号**，无论其中有多少个 diff 链式命中它。

---

## 5. 并发提交

- 每个目标路径一把锁，全局按路径排序获取；不同文件可完全并行。
- 后提交方基于 `state.json` 中的**最新版本**校验。请求可带
  `base_versions: {path: version}` 做乐观锁：版本不匹配返回
  `409 conflict`（`retryable=true`，含 `expected`/`actual`），调用方应以最新
  版本重试——**绝不会静默覆盖**他人修改。
- 即使不带 `base_versions`，应用仍针对最新内容定位；若在读取与提交之间文件被
  其他事务改动，补丁无法在旧内容上成立时会被转成可重试的 `conflict`。

---

## 6. 持久化与崩溃恢复

磁盘布局（根目录可配置，HTTP 默认 `$PATCH_ENGINE_ROOT` 或 `.patch-engine`）：

```
store/blobs/<sha256>     不可变内容对象
store/work/<relpath>     当前工作副本
store/state.json         已提交状态指针（原子 rename）
store/state.json.tmp.*   暂存指针（恢复时清理）
wal/log.jsonl            PREPARE / COMMIT 日志
```

WAL 协议与恢复：

| 崩溃时刻 | 磁盘现象 | 重启 `recover()` 行为 |
| --- | --- | --- |
| PREPARE 之前 | 无 PREPARE；可能残留 `state.json.tmp.*` | 清理暂存文件；state 仍是旧版本，等同回滚 |
| PREPARE 之后、state 发布前 | 有 PREPARE 无 COMMIT，blob 已落盘 | 按 PREPARE 清单幂等地重建工作副本与 state，补写 COMMIT |
| state 发布后、COMMIT 之前 | 有 PREPARE 无 COMMIT，state 可能已新 | 幂等重放清单并补写 COMMIT（结果一致） |
| COMMIT 之后 | 完整 | 无需动作 |

- 最后一条日志若被崩溃截断（半行），WAL 打开时自动丢弃撕裂尾行。
- 恢复是幂等的：blob 内容寻址、state 原子发布，重复执行得到同一状态。
- 启动时调用 `PatchService(root).recover()`（HTTP 服务默认在创建时调用）。

---

## 7. 大文件与有界内存

小文件走内存路径；超过阈值（默认 4 MiB，`large_file_bytes`）的**单个 FilePatch**
自动走两遍流式路径（`patch_engine/stream.py`）：

- **Pass 1（定位）**：64 KiB 块迭代读取，只保留活动 hunk 的滚动窗口：
  - 在声明位置精确命中的 hunk 读到即定稿、其区域随即释放；
  - 需要最近/模糊匹配的 hunk 保留 `[声明位置 - hunk长度, EOF]` 区间。
- **Pass 2（重写）**：块拷贝到临时文件，命中区间替换为 post-image，
  随后流式哈希入 blob、流式复制到工作副本。

因此提交期间的额外 Python 堆占用约为
`O(块内行数 + 活动 hunk 覆盖区间)`，与**活动补丁数量/规模**相关，
而不是整文件大小。实测 40 万行（约 5.6 MiB）单行修改，提交期 Python 堆峰值
约 0.8 MiB。

### 可观测的处理时间上限

- 请求可传 `deadline_ms`；在解析后、各文件应用之间、持久化前等检查点比较
  `time.monotonic()`，超预算返回 `504 deadline_exceeded`，且在任何持久化之前
  中止（无半应用）。
- 每次提交结果含 `elapsed_ms`；服务维护指标：
  `batches_committed / batches_rolled_back / batches_recovered /
  last_elapsed_ms / max_elapsed_ms`（`GET /health`）。

---

## 8. HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 存活与指标 |
| `POST /recover` | 触发崩溃恢复，返回 `{recovered: batch_id 或 ""}` |
| `GET /files/{path}/status` | `exists/version/blob/size` |
| `GET /files/{path}` | 文件内容（404 若不存在） |
| `POST /batches` | 原子提交一批 diff |

批次请求体：

```json
{
  "diffs": ["--- a/f\n+++ b/f\n@@ ..."],
  "base_versions": {"f": 3},
  "batch_id": "client-id",
  "deadline_ms": 500
}
```

状态码：`200` 成功；`422` 解析错误；`409` 补丁被拒/版本冲突（`retryable`）；
`504` 超出时间预算。

启动：

```bash
uv run uvicorn main:app --reload
# 或指定数据目录
PATCH_ENGINE_ROOT=/var/lib/patch-engine uv run uvicorn main:app
```

程序化使用：

```python
from patch_engine.service import PatchService

svc = PatchService("./.patch-engine")
svc.recover()
result = svc.submit_batch(
    [diff_text],
    base_versions={"f.txt": 1},     # 可选乐观锁
    batch_id="my-batch",            # 可选，幂等键
    deadline_ms=1000,               # 可选时间预算
)
print(result.batch_id, result.elapsed_ms)
```

---

## 9. 测试

```bash
uv sync --group dev
uv run pytest
```

覆盖：

- 上下文冲突（fuzz 0/1/2、首尾上下文、删除目标缺失）
- 重复应用幂等（整文件、逐 hunk、批次 id 重放）
- 批次回滚（第二补丁失败、解析失败、越界）
- 并发竞争（20 线程追加不丢更新、过期 base 被拒、独立文件全并行）
- 崩溃恢复（PREPARE/workfiles/state/COMMIT 各注入点、撕裂 WAL、重启续跑）
- 大文件（20 万/40 万行正确性、流式与内存版等价、堆峰值有界、失败回滚、偏移匹配）
- HTTP 状态码与可区分拒绝原因
