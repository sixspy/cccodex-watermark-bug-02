# 残段提交边界 · 复现步骤纸

对象：`watermark-bug-02`（血缘水印台）· 端口 7510 · admin / 123456

事故口径：盖印接口循环中途抛错，前几行水印已提交、后几行空白，页面却报
**全成功**；缺口对账才露出残段，入库数字对不上。

本纸按「四处同查」列出复现与验收动作：**残段目录 / 批次列表 / 盖印附页 /
对账席**。每一处都必须看到同一枚残段芯片。

---

## 0. 启动

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # http://127.0.0.1:7000/login
# 或
docker compose up -d --build     # http://127.0.0.1:7510/login
```

登录后进入「批次」页。初始有 3 个待盖印批次（每批 4 行）与 1 个已盖印批次。

---

## 1. 复现旧缺陷（不走修复路径）

修复前 `POST /api/batches/{id}/stamp` 由 `app/services/partial_stamp.py` 处理：
先 `UPDATE run_batches` 并 `conn.commit()`，再直接返回
`{"ok": true, "message": "全成功"}`。结果是批次已记「已盖印」、逐行数据却是空的，
对账时才出现缺口。修复后该文件已删除，`stamp_batch` 只走事务化边界。

旧代码的等价回放：在旧快照上对 `ord-20261114-01` 点「写入盖印」，观察接口 200 +
「全成功」，但 `run_batches` 行与 `stamp_rows` 行对不上。

---

## 2. 复现与验收：回滚边界（默认）

1. 「批次」页选中 `ord-20261114-01` → 点「盖印」。
2. 勾选 **中断演练**，行号填 `2`；**残段提交边界** 选「整笔回滚」。
3. 点「写入盖印」。

**预期（修复后）**

| 检查点 | 结果 |
|--------|------|
| 页面提示 | 红字：`盖印中断：已提交 2 行、待盖 2 行，已登记残段 #1（边界：回滚）…`，**不再出现「全成功」** |
| 批次列表 | 状态仍「待盖印」，芯片 `残段·回滚` |
| 盖印附页 | 4 行依次为 `已盖 / 已盖 / 待盖 / 待盖`；残段区列出 `#1 · 残段·回滚` |
| 残段目录 | 新增 1 行为「待续写」，芯片 `残段·回滚`，已提交 2 / 待盖 2 / 回滚 0 |
| 对账席 | 摘要含 `残段·回滚`，待续写残段 1 处；行已盖 6（含本批 2 行） |

4. 到「残段」页对 `#1` 点「续写收口」。

**预期**

- 提示：`续写收口（回滚）：已提交行恢复待盖，残段已标记「已收口·回滚」，缺口回升`
- 残段目录：芯片变 `已收口·回滚`，回滚 2；编排任务落档新增 `续写 · 回滚收口`
- 盖印附页：4 行全部回到 `待盖`，批次态 `未盖`，批次仍在「待盖印」
- 对账席：行已盖数 **减少 2**，缺口 **回升 2**，摘要含 `已收口·回滚（已整笔撤回）`
- 再次点「续写收口」→ 409 `残段 #1 已收口（回滚），不能重复续写`

> 关键：回滚残段**不占库存**——已提交行退回缺口。

---

## 3. 复现与验收：留存边界

1. 重新对同一批次勾选「中断演练」行号 `2`，边界选「部分留存」，点「写入盖印」。
2. 残段目录出现 `残段·留存`；批次芯片同源显示 `残段·留存`。
3. 点「续写收口」。

**预期**

- 提示：`续写收口（留存）：已提交行保持已盖并占库存，补盖任务 MK-… 承接剩余 2 行`
- 批次态转为 `待补盖`，批次列表芯片 `补盖·待补盖`
- 残段目录该行出现「补盖剩余码」按钮，任务码 `MK-ord-20261114-01-N`，剩 2 行
- 对账席：摘要含 `留存 2 行仍占库存` 与 `待补盖任务 1 张`；**行已盖数不变**
- 点「补盖剩余码」→ 提示 `补盖完成：剩余码已写完，批次收为「已盖印」`
- 盖印附页 4 行全部 `已盖`，批次 `已盖印` / `已盖`；对账行已盖数 **+2**，缺口 **-2**

> 关键：留存残段**占库存**——已提交行计入已盖，直到补盖才收口成整批。
> 选边不同，同一残段对库存/缺口的影响方向相反，这是本题的工程结局分叉点。

---

## 4. 同源校验（漂移拦截）

任何写入口（盖印 / 续写 / 补盖 / 对账）都会先过 `app/services/consistency.py`。
批次态只允许四档：`未盖 / 残段 / 待补盖 / 已盖`，且必须与残段行数互为镜像。

手工制造漂移（演示用）：

```bash
sqlite3 instance/watermark_lab.db \
  "UPDATE run_batches SET stamp_state='已盖' WHERE id=1;"
```

**预期**

- 「批次」页顶部出现红色横幅「残段行与批次态漂移，写入口已全部拦截」，列出具体漂移项
- 盖印附页同位置出现本批漂移明细
- 点「续写收口」→ 409，错误含「漂移」，**未落库**
- 对账席点「生成对账」→ 409 `blocked: true`，不生成假摘要
- 其他未漂移批次仍可正常盖印

---

## 5. 并发点续写（排他锁）

两侧值班同时点同一处以「留存」登记的残段：

- 续写在 `BEGIN IMMEDIATE` 写事务内完成「行 + 批次态 + 残段收口 + 补盖任务」
- 两笔并发只有一笔成功；另一笔收到 409 `残段 #N 已收口（留存），不能重复续写`
- `makeup_tasks` 中该残段**只有一行**，`stamp_rows` 不会被双写

自动化覆盖：`tests/test_residue_boundary.py::
test_concurrent_resume_writes_residue_once` 与
`::test_resume_serialises_behind_write_lock`（后者直接证明写锁会让第二个续写阻塞排队）。

---

## 6. 缺口与补盖各复验一轮

```bash
.venv/bin/python -m pytest -q
```

- 缺口复验：`test_rollback_residue_no_longer_occupies_stock`、
  `test_retain_residue_occupies_stock_until_makeup`、`test_drift_between_rows_and_batch_state_blocks_writes`
- 补盖复验：`test_retain_boundary_keeps_rows_and_opens_makeup_task`、
  `test_makeup_codes_must_match_pending_rows`、`test_retain_resume_and_makeup_via_api`
- 页面收口芯片：`test_interrupt_reports_failure_not_success` 断言批次列表、
  附页、残段目录、对账摘要出现同一枚 `残段·回滚` 芯片
- 编排任务落档：残段页「编排任务落档」表记录 `续写 · 回滚收口` /
  `续写 · 留存收口` / `补盖 · 写剩余码`

---

## 7. 落库位置速查

| 关注点 | 代码 |
|--------|------|
| 提交边界（回滚 / 留存一次落库） | `app/services/residues.py::resume_residue` |
| 中断时登记残段、拒绝假成功 | `app/services/batches.py::stamp_batch` |
| 同源校验（漂移即拦） | `app/services/consistency.py` |
| 排他写锁 | `app/db.py::immediate_transaction`（`BEGIN IMMEDIATE`） |
| 补盖任务写剩余码 | `app/services/residues.py::fill_makeup_task` |
| 对账席同源摘要 | `app/services/gaps.py::run_gap_report` |
| 残段目录 / 附页接口 | `app/main.py` `/api/residues*`、`/api/batches/{id}/sheet` |
