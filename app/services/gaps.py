"""缺口对账。

对账席与残段目录同源：摘要里必须露出残段与留存行数，缺口既按批次算
也按行算；任何残段行与批次态漂移都会先被拦截，不生成假「全成功」摘要。
"""

from app.db import get_db, immediate_transaction
from app.services.consistency import (
    CLOSED_RESIDUE,
    OPEN_RESIDUE,
    PENDING_TASK,
    consistency_issues,
)
from app.services.errors import ServiceError


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM gap_reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def _markers(conn, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, b.run_code, b.stamp_state FROM residue_segments r "
        "JOIN run_batches b ON b.id = r.batch_id "
        "ORDER BY r.id",
    ).fetchall()
    return [
        {
            "residue_id": row["id"],
            "run_code": row["run_code"],
            "batch_id": row["batch_id"],
            "boundary": row["boundary"],
            "state": row["state"],
            "committed_rows": row["committed_rows"],
            "pending_rows": row["pending_rows"],
            "rolled_back_rows": row["rolled_back_rows"],
            "marker": (
                f"残段·{row['boundary']}"
                if row["state"] == OPEN_RESIDUE
                else f"已收口·{row['boundary']}"
            ),
        }
        for row in rows
    ]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    with immediate_transaction(db_path) as conn:
        drift = []
        for row in conn.execute("SELECT id FROM run_batches ORDER BY id").fetchall():
            drift.extend(consistency_issues(conn, row["id"]))
        if drift:
            raise ServiceError(
                "对账已拦截：残段行与批次态漂移（" + "；".join(drift) + "），未生成对账",
                409,
                {"blocked": True, "issues": drift},
            )

        expected = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status!='已作废'",
            (day,),
        ).fetchone()["c"]
        stamped = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status='已盖印'",
            (day,),
        ).fetchone()["c"]
        expected_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_rows s JOIN run_batches b ON b.id=s.batch_id "
            "WHERE b.biz_date<=? AND b.status!='已作废'",
            (day,),
        ).fetchone()["c"]
        stamped_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_rows s JOIN run_batches b ON b.id=s.batch_id "
            "WHERE b.biz_date<=? AND b.status!='已作废' AND s.row_status='已盖'",
            (day,),
        ).fetchone()["c"]
        open_residues = conn.execute(
            "SELECT boundary, COUNT(*) AS c, SUM(pending_rows) AS pending "
            "FROM residue_segments r JOIN run_batches b ON b.id=r.batch_id "
            "WHERE b.biz_date<=? AND b.status!='已作废' AND r.state=? GROUP BY boundary",
            (day, OPEN_RESIDUE),
        ).fetchall()
        residue_count = sum(r["c"] for r in open_residues)
        detail_notes: list[str] = []
        for row in open_residues:
            detail_note = (
                "已提交行暂记库存，续写时整笔撤回"
                if row["boundary"] == "回滚"
                else "已提交行占库存，续写只承接补盖任务"
            )
            detail_notes.append(
                f"待续写残段·{row['boundary']} {row['c']} 处"
                f"（待盖 {row['pending'] or 0} 行，{detail_note}）"
            )
        retained_rows = conn.execute(
            "SELECT COALESCE(SUM(r.committed_rows), 0) AS c FROM residue_segments r "
            "JOIN run_batches b ON b.id=r.batch_id "
            "WHERE b.biz_date<=? AND b.status!='已作废' AND r.state=? AND r.boundary='留存'",
            (day, CLOSED_RESIDUE),
        ).fetchone()["c"]
        pending_tasks = conn.execute(
            "SELECT COUNT(*) AS c FROM makeup_tasks WHERE status=?", (PENDING_TASK,)
        ).fetchone()["c"]

        gap = expected - stamped
        gap_rows = expected_rows - stamped_rows
        detail = (
            f"截至 {day}：应盖印 {expected} 批 / {expected_rows} 行，"
            f"已盖印 {stamped} 批 / {stamped_rows} 行，"
            f"缺口 {gap} 批 / {gap_rows} 行"
        )
        if retained_rows:
            retained_notes = conn.execute(
                "SELECT r.boundary, COUNT(*) AS c, SUM(r.committed_rows) AS rows "
                "FROM residue_segments r JOIN run_batches b ON b.id=r.batch_id "
                "WHERE b.biz_date<=? AND b.status!='已作废' AND r.state=? GROUP BY r.boundary",
                (day, CLOSED_RESIDUE),
            ).fetchall()
            for row in retained_notes:
                detail_notes.append(
                    f"已收口残段·{row['boundary']} {row['c']} 处"
                    + (
                        f"（留存 {row['rows']} 行仍占库存）"
                        if row["boundary"] == "留存"
                        else "（已整笔撤回）"
                    )
                )
        if pending_tasks:
            detail_notes.append(f"待补盖任务 {pending_tasks} 张")
        markers = _markers(conn, day)
        if detail_notes:
            detail += "；" + "；".join(detail_notes)
        if markers:
            detail += "；标记：" + "、".join(
                f"{m['run_code']} {m['marker']}" for m in markers
            )
        else:
            detail += "；无残段"

        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, gap_count, "
            "expected_rows, stamped_rows, gap_rows, residue_count, retained_rows, summary) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                day,
                expected,
                stamped,
                gap,
                expected_rows,
                stamped_rows,
                gap_rows,
                residue_count,
                retained_rows,
                detail,
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        report = dict(row)
        report["markers"] = markers
        report["pending_makeup_tasks"] = pending_tasks
        return report
