"""批次盖印与残段提交边界。

盖印逐行落库；一旦中途失败，不再假装全成功，而是把已提交行、待盖行和
所选边界（回滚 / 留存）登记成「残段」，交给残段目录续写。续写时后端
按边界在**一个** ``BEGIN IMMEDIATE`` 事务里一次落库：

  回滚 → 已提交水印行整笔恢复待盖，残段收口标记「回滚」，批次回到未盖；
  留存 → 已提交行保持已盖并另开补盖任务写剩余码，残段收口标记「留存」，
         批次转入待补盖。

选边因此决定残段是否仍占库存：留存行计入已盖，回滚行退回缺口。
"""

import json
import os

from app.config import Config
from app.db import get_db, immediate_transaction
from app.services.consistency import (
    BOUNDARIES,
    BOUNDARY_RETAIN,
    OPEN_RESIDUE,
    PENDING_TASK,
    STATE_RESIDUE,
    STATE_STAMPED,
    assert_consistent,
)
from app.services.errors import ServiceError


def strip_value(value: str | None) -> str:
    return (value or "").strip()


def residue_dict(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["marker"] = f"残段·{data['boundary']}"
    data["closed_marker"] = f"已收口·{data['boundary']}"
    return data


def task_dict(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    try:
        data["remaining_codes"] = json.loads(data["remaining_codes"] or "[]")
    except (TypeError, ValueError):
        data["remaining_codes"] = []
    return data


def batch_payload(conn, row) -> dict:
    data = dict(row)
    residue = conn.execute(
        "SELECT * FROM residue_segments WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    task = conn.execute(
        "SELECT * FROM makeup_tasks WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    data["residue"] = residue_dict(residue)
    data["makeup_task"] = task_dict(task)
    if residue and residue["state"] == OPEN_RESIDUE:
        data["chip"] = residue_dict(residue)["marker"]
        data["chip_kind"] = "residue"
    elif task and task["status"] == PENDING_TASK:
        data["chip"] = "补盖·待补盖"
        data["chip_kind"] = "makeup"
    else:
        data["chip"] = data["stamp_state"]
        data["chip_kind"] = "state"
    return data


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        return [batch_payload(conn, r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        return batch_payload(conn, row) if row else None


def get_batch_sheet(batch_id: int, db_path: str | None = None) -> dict | None:
    """盖印附页：批次、逐行状态、残段与补盖任务同源返回，供一处核对。"""

    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = batch_payload(conn, row)
        batch["rows"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM stamp_rows WHERE batch_id=? ORDER BY row_no", (batch_id,)
            ).fetchall()
        ]
        batch["residues"] = [
            residue_dict(r)
            for r in conn.execute(
                "SELECT * FROM residue_segments WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
        batch["makeup_tasks"] = [
            task_dict(r)
            for r in conn.execute(
                "SELECT * FROM makeup_tasks WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
        batch["consistency"] = _issues(conn, batch_id)
        return batch


def _issues(conn, batch_id: int) -> list[str]:
    from app.services.consistency import consistency_issues

    return consistency_issues(conn, batch_id)


def resolve_boundary(boundary: str | None) -> str:
    chosen = strip_value(boundary) or Config.RESIDUE_BOUNDARY
    if chosen not in BOUNDARIES:
        raise ServiceError("残段提交边界只能是「回滚」或「留存」")
    return chosen


def resolve_fail_after(fail_after: int | None) -> int | None:
    if fail_after is None:
        raw = os.environ.get("STAMP_FAIL_AFTER", "").strip()
        if not raw:
            return None
        fail_after = raw
    try:
        return max(0, int(fail_after))
    except (TypeError, ValueError):
        raise ServiceError("演练中断行号必须是整数")


def _pending_codes(conn, batch_id: int) -> list[str]:
    return [
        r["partition_code"]
        for r in conn.execute(
            "SELECT partition_code FROM stamp_rows WHERE batch_id=? AND row_status='待盖' "
            "ORDER BY row_no",
            (batch_id,),
        ).fetchall()
    ]


def _insert_residue(
    conn,
    batch_id: int,
    run_code: str,
    value: str,
    boundary: str,
    committed: int,
    total: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO residue_segments(batch_id, run_code, watermark_value, boundary, "
        "committed_rows, pending_rows, state, detail) VALUES (?,?,?,?,?,?,?,?)",
        (
            batch_id,
            run_code,
            value,
            boundary,
            committed,
            total - committed,
            OPEN_RESIDUE,
            f"第 {committed + 1} 行中断：已提交 {committed} 行，待盖 {total - committed} 行",
        ),
    )
    residue_id = cur.lastrowid
    conn.execute(
        "UPDATE run_batches SET stamp_state=? WHERE id=?", (STATE_RESIDUE, batch_id)
    )
    if boundary == BOUNDARY_RETAIN:
        # 留存边界的已提交行立刻占库存，剩余码当场落档成补盖任务，续写只做收口。
        codes = _pending_codes(conn, batch_id)
        conn.execute(
            "INSERT INTO makeup_tasks(residue_id, batch_id, run_code, task_code, "
            "remaining_codes, remaining_rows, status) VALUES (?,?,?,?,?,?,?)",
            (
                residue_id,
                batch_id,
                run_code,
                f"MK-{run_code}-{residue_id}",
                json.dumps(codes, ensure_ascii=False),
                len(codes),
                PENDING_TASK,
            ),
        )
        conn.execute(
            "UPDATE run_batches SET watermark_value=? WHERE id=?", (value, batch_id)
        )
    return residue_id


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
    *,
    boundary: str | None = None,
    fail_after: int | None = None,
) -> dict:
    """整批盖印；中断时登记同源残段，绝不返回「全成功」。"""

    value = strip_value(watermark_value)
    if not value:
        raise ServiceError("水印值不能为空")
    chosen = resolve_boundary(boundary)
    fail_at = resolve_fail_after(fail_after)
    interrupted: ServiceError | None = None

    with immediate_transaction(db_path) as conn:
        batch = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        open_residue = conn.execute(
            "SELECT * FROM residue_segments WHERE batch_id=? AND state=?",
            (batch_id, OPEN_RESIDUE),
        ).fetchone()
        if open_residue:
            raise ServiceError(
                f"批次仍有待续写残段 #{open_residue['id']}，请先到残段目录点续写收口", 409
            )
        pending_task = conn.execute(
            "SELECT * FROM makeup_tasks WHERE batch_id=? AND status=?",
            (batch_id, PENDING_TASK),
        ).fetchone()
        if pending_task:
            raise ServiceError(
                f"批次有未完成的补盖任务 {pending_task['task_code']}，请先补盖剩余码", 409
            )
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (batch["table_name"],),
        ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)
        assert_consistent(conn, batch_id, "盖印")

        rows = conn.execute(
            "SELECT * FROM stamp_rows WHERE batch_id=? ORDER BY row_no", (batch_id,)
        ).fetchall()
        if not rows:
            raise ServiceError("批次没有可盖印的行", 409)
        total = len(rows)

        if fail_at is not None and fail_at < total:
            for row in rows[:fail_at]:
                conn.execute(
                    "UPDATE stamp_rows SET row_status='已盖', watermark_value=?, "
                    "stamped_at=datetime('now') WHERE id=?",
                    (value, row["id"]),
                )
            residue_id = _insert_residue(
                conn, batch_id, batch["run_code"], value, chosen, fail_at, total
            )
            assert_consistent(conn, batch_id, "盖印中断登记")
            # 中断登记本身就是本事务的成果：先提交，事务外再向调用方报错，
            # 否则残段会随异常一起回滚，页面又变成「全成功」。
            interrupted = ServiceError(
                f"盖印中断：已提交 {fail_at} 行、待盖 {total - fail_at} 行，"
                f"已登记残段 #{residue_id}（边界：{chosen}）。"
                f"请到残段目录点续写完成收口。",
                409,
                {
                    "residue_id": residue_id,
                    "committed_rows": fail_at,
                    "pending_rows": total - fail_at,
                    "boundary": chosen,
                },
            )
        else:
            for row in rows:
                conn.execute(
                    "UPDATE stamp_rows SET row_status='已盖', watermark_value=?, "
                    "stamped_at=datetime('now') WHERE id=?",
                    (value, row["id"]),
                )
            conn.execute(
                "UPDATE run_batches SET status='已盖印', stamp_state=?, watermark_value=?, "
                "stamped_at=datetime('now') WHERE id=?",
                (STATE_STAMPED, value, batch_id),
            )
            assert_consistent(conn, batch_id, "盖印落库")
            result = batch_payload(
                conn,
                conn.execute(
                    "SELECT * FROM run_batches WHERE id=?", (batch_id,)
                ).fetchone(),
            )

    if interrupted is not None:
        raise interrupted
    return result
