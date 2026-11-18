"""残段行与批次态的同源检查。

任何写入口（盖印 / 续写 / 补盖）与对账前都先过这里；一旦发现残段行数与
批次态对不上，直接拦截，不再往前写。

批次态 ``stamp_state`` 只有四档，与行状态互为镜像：

  未盖   → 0 行已盖，无残段、无补盖任务
  残段   → 恰好一处待续写残段；其已提交行数 == 实际已盖行数
  待补盖 → 无待续写残段，恰有一张待补盖任务，剩余码 == 实际待盖行
  已盖   → 全部行已盖，无残段、无补盖任务
"""

import json

OPEN_RESIDUE = "待续写"
CLOSED_RESIDUE = "已收口"
BOUNDARY_ROLLBACK = "回滚"
BOUNDARY_RETAIN = "留存"
BOUNDARIES = (BOUNDARY_ROLLBACK, BOUNDARY_RETAIN)
PENDING_TASK = "待补盖"
DONE_TASK = "已补盖"
VOID_TASK = "已作废"
STATE_UNSTAMPED = "未盖"
STATE_RESIDUE = "残段"
STATE_MAKEUP = "待补盖"
STATE_STAMPED = "已盖"


def _task_codes(task) -> set[str]:
    try:
        return set(json.loads(task["remaining_codes"] or "[]"))
    except (TypeError, ValueError):
        return set()


def consistency_issues(conn, batch_id: int) -> list[str]:
    batch = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        return [f"批次 #{batch_id} 不存在"]

    issues: list[str] = []
    run_code = batch["run_code"]
    rows = conn.execute(
        "SELECT * FROM stamp_rows WHERE batch_id=? ORDER BY row_no", (batch_id,)
    ).fetchall()
    total = len(rows)
    stamped = [r for r in rows if r["row_status"] == "已盖"]
    pending_codes = sorted(r["partition_code"] for r in rows if r["row_status"] == "待盖")
    state = batch["stamp_state"]

    open_residue = conn.execute(
        "SELECT * FROM residue_segments WHERE batch_id=? AND state=?",
        (batch_id, OPEN_RESIDUE),
    ).fetchone()
    open_count = conn.execute(
        "SELECT COUNT(*) AS c FROM residue_segments WHERE batch_id=? AND state=?",
        (batch_id, OPEN_RESIDUE),
    ).fetchone()["c"]
    pending_task = conn.execute(
        "SELECT * FROM makeup_tasks WHERE batch_id=? AND status=?",
        (batch_id, PENDING_TASK),
    ).fetchone()
    pending_task_count = conn.execute(
        "SELECT COUNT(*) AS c FROM makeup_tasks WHERE batch_id=? AND status=?",
        (batch_id, PENDING_TASK),
    ).fetchone()["c"]

    if total == 0:
        issues.append(f"批次 {run_code} 没有任何盖印行")
    if open_count > 1:
        issues.append(f"批次 {run_code} 并存 {open_count} 处待续写残段（只允许一处）")
    if pending_task_count > 1:
        issues.append(f"批次 {run_code} 并存 {pending_task_count} 张待补盖任务（只允许一张）")

    # ---- 批次状态 与 批次态 ----
    if batch["status"] == "已盖印":
        if state != STATE_STAMPED:
            issues.append(f"批次 {run_code} 状态为已盖印，批次态却是「{state}」")
        if total and len(stamped) != total:
            issues.append(
                f"批次 {run_code} 状态为已盖印，但仅 {len(stamped)}/{total} 行已盖"
            )
    elif batch["status"] == "待盖印":
        if state == STATE_STAMPED:
            issues.append(f"批次 {run_code} 批次态为已盖，状态却仍是待盖印")
    else:
        if state not in (STATE_UNSTAMPED,) and batch["status"] == "已作废":
            issues.append(f"批次 {run_code} 已作废，批次态却为「{state}」")

    # ---- 残段 ----
    if open_residue:
        if state != STATE_RESIDUE:
            issues.append(
                f"存在待续写残段 #{open_residue['id']}，批次态为「{state}」（应为残段）"
            )
        if batch["status"] != "待盖印":
            issues.append(f"批次 {run_code} 仍有待续写残段，状态却是「{batch['status']}」")
        if len(stamped) != open_residue["committed_rows"]:
            issues.append(
                f"残段 #{open_residue['id']} 登记已提交 {open_residue['committed_rows']} 行，"
                f"实际已盖 {len(stamped)} 行"
            )
        if len(pending_codes) != open_residue["pending_rows"]:
            issues.append(
                f"残段 #{open_residue['id']} 登记待盖 {open_residue['pending_rows']} 行，"
                f"实际待盖 {len(pending_codes)} 行"
            )
        expected = total - open_residue["committed_rows"]
        if open_residue["pending_rows"] != expected:
            issues.append(
                f"残段 #{open_residue['id']} 已提交 {open_residue['committed_rows']} + "
                f"待盖 {open_residue['pending_rows']} ≠ 总行 {total}"
            )
        if open_residue["boundary"] == BOUNDARY_RETAIN:
            if (batch["watermark_value"] or None) != (open_residue["watermark_value"] or None):
                issues.append(
                    f"留存残段 #{open_residue['id']} 水印值与批次水印值不一致"
                )
            if not pending_task:
                issues.append(
                    f"留存残段 #{open_residue['id']} 未登记补盖任务（剩余码无处落档）"
                )
        elif open_residue["boundary"] == BOUNDARY_ROLLBACK:
            if batch["watermark_value"] is not None:
                issues.append(
                    f"回滚残段 #{open_residue['id']} 待续写，批次却已挂水印值（续写时应整笔回滚）"
                )
            if pending_task:
                issues.append(
                    f"回滚残段 #{open_residue['id']} 不应并存补盖任务 {pending_task['task_code']}"
                )
    else:
        if state == STATE_RESIDUE:
            issues.append(f"批次 {run_code} 批次态为残段，但不存在待续写残段")
        if pending_task and state != STATE_MAKEUP:
            issues.append(
                f"补盖任务 {pending_task['task_code']} 待补盖，批次 {run_code} 却既无"
                f"待续写残段、批次态也不是待补盖（现为「{state}」）"
            )

    # ---- 补盖任务 与 批次态 ----
    if state == STATE_MAKEUP:
        if batch["status"] != "待盖印":
            issues.append(f"批次 {run_code} 批次态为待补盖，状态却是「{batch['status']}」")
        if not pending_task:
            issues.append(f"批次 {run_code} 批次态为待补盖，却不存在待补盖任务")
        if len(stamped) == 0:
            issues.append(f"批次 {run_code} 批次态为待补盖，却没有任何已留存行")
    if pending_task and state not in (STATE_MAKEUP, STATE_RESIDUE):
        issues.append(
            f"补盖任务 {pending_task['task_code']} 待补盖，批次 {run_code} 批次态却是「{state}」"
        )
    if pending_task:
        if pending_task["remaining_rows"] != len(pending_codes):
            issues.append(
                f"补盖任务 {pending_task['task_code']} 登记剩余 {pending_task['remaining_rows']} 行，"
                f"实际待盖 {len(pending_codes)} 行"
            )
        if _task_codes(pending_task) != set(pending_codes):
            issues.append(
                f"补盖任务 {pending_task['task_code']} 剩余码 "
                f"{sorted(_task_codes(pending_task))} 与待盖行 {pending_codes} 不一致"
            )

    # ---- 未盖 / 已盖 的镜像 ----
    if state == STATE_UNSTAMPED:
        if stamped:
            issues.append(f"批次 {run_code} 批次态为未盖，却有 {len(stamped)} 行已盖")
        if batch["watermark_value"] is not None:
            issues.append(f"批次 {run_code} 批次态为未盖，却挂着水印值")
    if state == STATE_STAMPED:
        if open_residue:
            issues.append(f"批次 {run_code} 批次态为已盖，却仍有待续写残段")
        if pending_task:
            issues.append(f"批次 {run_code} 批次态为已盖，却仍有待补盖任务")
        if total and len(stamped) != total:
            issues.append(
                f"批次 {run_code} 批次态为已盖，但仅 {len(stamped)}/{total} 行已盖"
            )

    return issues


def assert_consistent(conn, batch_id: int, action: str) -> None:
    issues = consistency_issues(conn, batch_id)
    if issues:
        from app.services.errors import ServiceError

        raise ServiceError(
            f"{action}已拦截：残段行与批次态漂移（{'；'.join(issues)}），未落库",
            409,
            {"blocked": True, "issues": issues},
        )
