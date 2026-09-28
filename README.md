# patch-engine

本地补丁应用与冲突消解服务。接收 **unified diff** 补丁，对工作副本中的
多个文本文件执行修改，并保证：

- 行号越界、上下文不匹配等失败有**可区分的拒绝原因**，绝不使用错误位置继续；
- 上下文漂移时支持**确定性的最近模糊匹配**；重复应用幂等；
- 多个补丁组成**一个批次（事务）**，任一失败整体回滚，不留半应用状态；
- 对同一文件的并发提交基于**最新版本校验**，冲突时拒绝，不静默覆盖；
- 提交日志与工作副本状态**持久化**，崩溃/重启后可区分已提交与未提交批次；
- **流式大文件**处理，内存占用与活动补丁数量相关，处理时限可观测。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload          # HTTP 服务，默认工作目录 ./workspace
uv run pytest                             # 全部测试
```

```bash
curl -s -X POST localhost:8000/batches \
  -H 'content-type: application/json' \
  -d '{"patches":["--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi\n"]}'
```

也可直接在代码中使用：

```python
from patch_engine.service import PatchService

svc = PatchService("./workspace")
svc.recover()                                  # 启动时对账
result = svc.submit([open("change.patch").read()])
print(result.versions)                         # path -> 内容版本号(sha256)
```

## 支持的补丁格式

标准 unified diff（git 风格），包括：

- `--- a/path` / `+++ b/path` 文件头，`a/`、`b/` 前缀自动剥离；
- `/dev/null` 表示新建（旧侧为空）或删除（新侧为空）；
- `@@ -old_start,old_count +new_start,new_count @@` hunk 头与可选节标题；
- 上下文行（空格开头）、删除行（`-`）、新增行（`+`）；
- `\ No newline at end of file` 标记，保留文件尾换行语义；
- `new file mode`、`deleted file mode`、`rename from/to`、`old/new mode`。

畸形补丁（无法识别的头、hunk 行计数与内容不符等）一律以
`malformed_patch` 拒绝，不会产出部分解析结果。

## 应用与冲突判定规则

引擎在文件的**行流**上为每个 hunk 寻找落点，按以下优先级**确定性**选择：

1. **声明位置精确匹配**：hunk 头 `old_start` 处完整匹配旧侧模式
   （上下文 + 删除行）；
2. **幂等识别**：声明位置已经是新侧模式，判定该补丁此前已应用，保持现状；
3. **最近精确匹配**：在声明位置两侧 `fuzz_radius`（默认 200 行）窗口内
   搜索旧模式，取距离最近者；距离相同则向行号减小方向（backward）优先，
   再相同按起始行号，保证结果唯一且可复现；
4. **模糊匹配**：按 `fuzz_level`（默认 2，上限 3）逐档对称剥离 hunk
   **两端的上下文行**（不剥离变更行），重复步骤 3。选择剥离行数最少、
   距离最近的候选；被剥离的上下文保留文件原样，只替换核心变更区。

拒绝原因（互不混淆，见 `RejectReason`）：

| 原因 | 触发条件 |
| --- | --- |
| `hunk_range_out_of_bounds` | 非空旧模式的声明起始行已越过文件尾 |
| `context_mismatch` | 位置可达，但在模糊窗口内找不到任何匹配 |
| `hunks_overlap` | 某 hunk 的落点与已处理 hunk 的区间重叠 |
| `malformed_patch` | diff 文本不符合 unified diff 语法 |
| `target_exists` | CREATE 的目标已存在且内容不同 / RENAME 目标已存在 |
| `target_missing` | MODIFY/DELETE/RENAME 的源文件不存在 |
| `version_conflict` | 提交携带的期望版本不是最新版本（并发竞争） |
| `deadline_exceeded` | 应用耗时超过 `deadline_seconds` |

**关键原则：一旦某个 hunk 无法确定落点，立即拒绝整个文件补丁与整个批次；
引擎永远不会“猜一个位置”后继续。** 匹配阶段输出只写入临时目标，
原文件在全部校验通过前不会被触碰。

## 批次与原子性

- 一次 `submit` 是一个事务。批次内补丁按顺序作用于“批次视图”：
  同一文件的多个补丁顺序生效；不同文件彼此独立。
- **全部成功才发布**：所有新内容先写入 `staging/<batch_id>/`，
  通过校验后，清单 `manifest.json` 以同目录 `rename` 原子切换，
  再物化为工作副本。
- **任一失败即回滚**：丢弃暂存目录，工作副本、对象库与清单保持提交前状态。

### 顺序无关性

对**相互独立的文件**，同一组补丁以不同顺序提交，最终版本表一致
（见 `test_independent_patches_reorder_consistent`）。对同一文件的
补丁顺序敏感，因为它们构成先后变更链——这与补丁的文本语义一致。

### 幂等

- 同一补丁重复提交：引擎识别声明位置已是新侧模式，结果不变，
  返回 `no_change=true` 且版本号不变；
- CREATE 已存在且内容相同、DELETE 目标已不存在，同样视为已应用。

## 并发控制

所有提交在全局文件锁（`fcntl.flock`，跨进程；同进程可重入）内串行发布：

1. 进入锁后读取**最新清单**；
2. 若调用方提供 `expected_versions`，先做预检：路径的当前 sha256 版本
   必须等于期望值（路径不存在用 `null`），否则立即返回 `version_conflict`，
   早于任何内容应用；
3. 应用补丁、暂存 blob、在提交点再次基于最新清单校验，原子发布。

因此后提交方不会静默覆盖先提交方的修改：要么基于最新版本重试，
要么收到明确冲突。不同文件的并发补丁互不阻塞其内容校验，
仅在提交点短暂串行，二者均可成功。

## 崩溃恢复

磁盘布局：

```
<workspace>/
  wc/                 工作副本真实文件
  objects/<sha256>    不可变内容 blob
  manifest.json       当前版本清单 {path: blob_id} + last_batch
  staging/<bid>/      批次暂存（blobs/、候选 manifest）
  journal.log         仅追加 WAL（JSON Lines，逐条 fsync）
  locks/global.lock   提交锁
```

提交流程与 WAL：

1. 暂存全部新 blob；
2. journal 追加 `pending`（fsync）；
3. 校验版本 → 原子 `rename` 发布清单（**提交点**）→ 物化工作副本；
4. journal 追加 `committed`（fsync）。

启动时调用 `recover()`（创建服务时也会自动执行）对账：

- 日志最终为 `committed`，或 manifest 的 `last_batch` 已是该批次
  （崩溃发生在步骤 3 之后、步骤 4 之前）→ 认定**已提交**，
  补记 `committed`，并把工作副本重新对齐清单；
- 只有 `pending` 且清单未切换 → **未提交**，删除暂存，
  工作副本保持上一个一致状态；
- 日志尾部半截记录（写最后一行时断电）会被检测并截断；
- 无主暂存目录（崩溃发生在 `pending` 落盘之前）被清理。

因此恢复后每个批次非“已提交”即“未提交（已回滚）”，不存在
“部分文件已修改”的可见状态。

## 大文件与时限

- 引擎按行迭代输入、向临时输出流写入，只保留有界滑动窗口：
  窗口下界是上一已提交 hunk 的区间末尾，上界是当前 hunk 声明位置
  加 `fuzz_radius` 与模式长度。峰值内存约为
  `fuzz_radius × 活动 hunk 的模式规模`，**与文件总行数无关**。
- 每次提交返回每个文件的统计：`lines_scanned`、`elapsed_seconds`、
  `fuzzed_hunks`、`deadline_exceeded`，处理时间可观测；
- 传入 `deadline_seconds` 后，引擎周期性检查预算，超期以
  `deadline_exceeded` 中止（批次回滚，HTTP 408）。

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `POST /batches` | 提交批次，body：`{patches: string[], expected_versions?, fuzz_radius?, fuzz_level?, deadline_seconds?, batch_id?}` |
| `GET /versions` | 全部文件当前版本号 |
| `GET /files/{path}` | 读取文件原文 |
| `POST /recover` | 手动触发恢复对账 |
| `GET /healthz` | 存活探针 |

状态码：`200` 成功；`422` 补丁被拒绝（body 中含结构化 `reason` 与
hunk 细节）；`409` 版本冲突；`408` 超过处理时限。

## 模块结构

- `patch_engine/model.py`：数据模型与拒绝原因；
- `patch_engine/parser.py`：unified diff 解析；
- `patch_engine/engine.py`：流式匹配 / 模糊 / 幂等引擎；
- `patch_engine/journal.py`：仅追加提交日志；
- `patch_engine/store.py`：对象库、清单、暂存、原子发布、恢复、文件锁；
- `patch_engine/service.py`：批次事务与并发控制；
- `patch_engine/api.py`：HTTP 接口。
