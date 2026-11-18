"""残段目录、续写收口与补盖任务。

续写是残段提交边界的唯一落库点：按登记时选定的边界，在单个
``BEGIN IMMEDIATE`` 事务里一次完成「行 + 批次态 + 残段收口 + 补盖任务」。
双侧值班并发点续写时由写锁排他，不会双写残段行。
"""

import json

from app.db import get_db, immediate_transaction
from app.services.batches import batch_payload, residue_dict, task_dict
from app.services.consistency import (
    BOUNDARY_RETAIN,
    BOUNDARY_ROLLBACK,
    CLOSED_RESIDUE,
    DONE_TASK,
    OPEN_RESIDUE,
    PENDING_TASK,
    STATE_MAKEUP,
    STATE_STAMPED,
    STATE_UNSTAMPED,
    assert_consistent,
)
from app.services.errors import ServiceError


def _log(conn, event: str, *, batch_id=None, residue_id=None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO stamp_events(batch_id, residue_id, event, detail) VALUES (?,?,?,?)",
        (batch_id, residue_id, event, detail),
    )


def list_residues(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM residue_segments ORDER BY id DESC").fetchall()
        items = []
        for row in rows:
            data = residue_dict(row)
            batch = conn.execute(
                "SELECT * FROM run_batches WHERE id=?", (row["batch_id"],)
            ).fetchone()
            data["batch"] = batch_payload(conn, batch) if batch else None
            data["makeup_task"] = task_dict(
                conn.execute(
                    "SELECT * FROM makeup_tasks WHERE residue_id=?", (row["id"],)
                ).fetchone()
            )
            data["chip"] = (
                data["marker"] if data["state"] == OPEN_RESIDUE else data["closed_marker"]
            )
            items.append(data)
        return items


def get_residue(residue_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM residue_segments WHERE id=?", (residue_id,)
        ).fetchone()
        if not row:
            return None
        data = residue_dict(row)
        data["makeup_task"] = task_dict(
            conn.execute(
                "SELECT * FROM makeup_tasks WHERE residue_id=?", (residue_id,)
            ).fetchone()
        )
        return data


def _pending_codes(conn, batch_id: int) -> list[str]:
    return [
        r["partition_code"]
        for r in conn.execute(
            "SELECT partition_code FROM stamp_rows WHERE batch_id=? AND row_status='待盖' "
            "ORDER BY row_no",
            (batch_id,),
        ).fetchall()
    ]


def _resume_rollback(conn, residue, batch) -> None:
    """整笔回滚：已提交水印行恢复待盖，批次退回未盖，残段标记回滚。"""

    conn.execute(
        "UPDATE stamp_rows SET row_status='待盖', watermark_value=NULL, stamped_at=NULL "
        "WHERE batch_id=? AND row_status='已盖'",
        (batch["id"],),
    )
    conn.execute(
        "UPDATE run_batches SET stamp_state=?, watermark_value=NULL, stamped_at=NULL "
        "WHERE id=?",
        (STATE_UNSTAMPED, batch["id"]),
    )
    conn.execute(
        "UPDATE residue_segments SET state=?, rolled_back_rows=committed_rows, "
        "closed_at=datetime('now'), detail=? WHERE id=?",
        (
            CLOSED_RESIDUE,
            f"回滚收口：{residue['committed_rows']} 行水印已撤回并恢复待盖，缺口计数随之回升",
            residue["id"],
        ),
    )
    _log(
        conn,
        "residue_resume_rollback",
        batch_id=batch["id"],
        residue_id=residue["id"],
        detail=f"回滚 {residue['committed_rows']} 行",
    )


def _resume_retain(conn, residue, batch) -> None:
    """部分留存：已提交行保持已盖，另开补盖任务写剩余码，批次转入待补盖。"""

    codes = _pending_codes(conn, batch["id"])
    if not codes:
        raise ServiceError("留存残段已无待盖行，请核对残段登记", 409)
    task = conn.execute(
        "SELECT * FROM makeup_tasks WHERE residue_id=?", (residue["id"],)
    ).fetchone()
    if task and task["status"] == PENDING_TASK:
        conn.execute(
            "UPDATE makeup_tasks SET remaining_codes=?, remaining_rows=? WHERE id=?",
            (json.dumps(codes, ensure_ascii=False), len(codes), task["id"]),
        )
        task_code = task["task_code"]
    else:
        task_code = f"MK-{residue['run_code']}-{residue['id']}"
        conn.execute(
            "INSERT INTO makeup_tasks(residue_id, batch_id, run_code, task_code, "
            "remaining_codes, remaining_rows, status) VALUES (?,?,?,?,?,?,?)",
            (
                residue["id"],
                batch["id"],
                residue["run_code"],
                task_code,
                json.dumps(codes, ensure_ascii=False),
                len(codes),
                PENDING_TASK,
            ),
        )
    conn.execute(
        "UPDATE run_batches SET stamp_state=?, watermark_value=? WHERE id=?",
        (STATE_MAKEUP, residue["watermark_value"], batch["id"]),
    )
    conn.execute(
        "UPDATE residue_segments SET state=?, closed_at=datetime('now'), detail=? WHERE id=?",
        (
            CLOSED_RESIDUE,
            f"留存收口：{residue['committed_rows']} 行保持已盖并占库存，"
            f"补盖任务 {task_code} 承接剩余 {len(codes)} 行",
            residue["id"],
        ),
    )
    _log(
        conn,
        "residue_resume_retain",
        batch_id=batch["id"],
        residue_id=residue["id"],
        detail=f"留存 {residue['committed_rows']} 行，补盖任务 {task_code}",
    )


def resume_residue(residue_id: int, db_path: str | None = None) -> dict:
    """按残段登记边界一次落库收口；并发重复点续写只会有一笔成功。"""

    with immediate_transaction(db_path) as conn:
        residue = conn.execute(
            "SELECT * FROM residue_segments WHERE id=?", (residue_id,)
        ).fetchone()
        if not residue:
            raise ServiceError("残段不存在", 404)
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (residue["batch_id"],)
        ).fetchone()
        if not batch:
            raise ServiceError("残段所属批次不存在", 404)
        if residue["state"] != OPEN_RESIDUE:
            raise ServiceError(
                f"残段 #{residue_id} 已收口（{residue['boundary']}），不能重复续写", 409
            )
        assert_consistent(conn, batch["id"], "续写前校验")

        if residue["boundary"] == BOUNDARY_ROLLBACK:
            _resume_rollback(conn, residue, batch)
        elif residue["boundary"] == BOUNDARY_RETAIN:
            _resume_retain(conn, residue, batch)
        else:  # pragma: no cover - schema CHECK guards this
            raise ServiceError("残段边界非法", 409)

        assert_consistent(conn, batch["id"], "续写落库")
        fresh_batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch["id"],)
        ).fetchone()
        return {
            "boundary": residue["boundary"],
            "residue": residue_dict(
                conn.execute(
                    "SELECT * FROM residue_segments WHERE id=?", (residue_id,)
                ).fetchone()
            ),
            "batch": batch_payload(conn, fresh_batch),
            "makeup_task": task_dict(
                conn.execute(
                    "SELECT * FROM makeup_tasks WHERE residue_id=?", (residue_id,)
                ).fetchone()
            ),
        }


def fill_makeup_task(
    task_id: int,
    watermark_value: str | None = None,
    db_path: str | None = None,
) -> dict:
    """补盖任务写剩余码；全部写满后才把批次收成已盖印。"""

    with immediate_transaction(db_path) as conn:
        task = conn.execute(
            "SELECT * FROM makeup_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not task:
            raise ServiceError("补盖任务不存在", 404)
        if task["status"] == DONE_TASK:
            raise ServiceError(f"补盖任务 {task['task_code']} 已补盖", 409)
        if task["status"] != PENDING_TASK:
            raise ServiceError(f"补盖任务 {task['task_code']} 已作废", 409)
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (task["batch_id"],)
        ).fetchone()
        if not batch:
            raise ServiceError("补盖任务所属批次不存在", 404)
        residue = conn.execute(
            "SELECT * FROM residue_segments WHERE id=?", (task["residue_id"],)
        ).fetchone()
        if not residue or residue["state"] != CLOSED_RESIDUE:
            raise ServiceError("补盖任务对应残段尚未收口", 409)
        value = (watermark_value or "").strip() or (residue["watermark_value"] or "")
        if not value:
            raise ServiceError("水印值不能为空")
        assert_consistent(conn, batch["id"], "补盖前校验")

        try:
            codes = list(json.loads(task["remaining_codes"] or "[]"))
        except (TypeError, ValueError):
            codes = []
        pending = conn.execute(
            "SELECT * FROM stamp_rows WHERE batch_id=? AND row_status='待盖' ORDER BY row_no",
            (batch["id"],),
        ).fetchall()
        pending_codes = [r["partition_code"] for r in pending]
        if codes != pending_codes:
            raise ServiceError("补盖任务剩余码与待盖行不一致，已拦截", 409)

        for row in pending:
            conn.execute(
                "UPDATE stamp_rows SET row_status='已盖', watermark_value=?, "
                "stamped_at=datetime('now') WHERE id=?",
                (value, row["id"]),
            )
        remaining_codes = _pending_codes(conn, batch["id"])
        if remaining_codes:
            conn.execute(
                "UPDATE makeup_tasks SET remaining_codes=?, remaining_rows=? WHERE id=?",
                (
                    json.dumps(remaining_codes, ensure_ascii=False),
                    len(remaining_codes),
                    task_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE makeup_tasks SET status=?, remaining_codes='[]', remaining_rows=0, "
                "finished_at=datetime('now') WHERE id=?",
                (DONE_TASK, task_id),
            )
            conn.execute(
                "UPDATE run_batches SET status='已盖印', stamp_state=?, watermark_value=?, "
                "stamped_at=COALESCE(stamped_at, datetime('now')) WHERE id=?",
                (STATE_STAMPED, value, batch["id"]),
            )
        _log(
            conn,
            "makeup_filled",
            batch_id=batch["id"],
            residue_id=residue["id"],
            detail=f"补盖 {len(pending)} 行，剩余 {len(remaining_codes)} 行",
        )
        assert_consistent(conn, batch["id"], "补盖落库")
        return {
            "task": task_dict(
                conn.execute("SELECT * FROM makeup_tasks WHERE id=?", (task_id,)).fetchone()
            ),
            "batch": batch_payload(
                conn,
                conn.execute(
                    "SELECT * FROM run_batches WHERE id=?", (batch["id"],)
                ).fetchone(),
            ),
            "filled_rows": len(pending),
            "remaining_rows": len(remaining_codes),
        }


def list_makeup_tasks(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM makeup_tasks ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            data = task_dict(row)
            data["residue"] = residue_dict(
                conn.execute(
                    "SELECT * FROM residue_segments WHERE id=?", (row["residue_id"],)
                ).fetchone()
            )
            result.append(data)
        return result


def list_events(batch_id: int | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if batch_id is None:
            rows = conn.execute(
                "SELECT * FROM stamp_events ORDER BY id DESC LIMIT 200"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stamp_events WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [dict(r) for r in rows]
