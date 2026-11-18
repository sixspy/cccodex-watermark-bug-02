"""残段提交边界验收：回滚 / 留存两条工程结局、同源校验、并发续写排他。"""

import sqlite3
import threading

import pytest

from app.db import connect
from app.services import (
    fill_makeup_task,
    get_batch_sheet,
    list_batches,
    list_makeup_tasks,
    list_residues,
    resume_residue,
    run_gap_report,
    stamp_batch,
)
from app.services.errors import ServiceError


def _pending_batch(authed_batches):
    return next(b for b in authed_batches if b["status"] == "待盖印")


def _rows(sheet):
    return [(r["row_status"], r["watermark_value"]) for r in sheet["rows"]]


def _interrupt(batch_id, value, boundary, fail_after=2, db_path=None):
    with pytest.raises(ServiceError) as exc:
        stamp_batch(
            batch_id, value, db_path, boundary=boundary, fail_after=fail_after
        )
    err = exc.value
    assert err.status_code == 409
    assert "全成功" not in err.message
    assert err.payload["residue_id"]
    return err.payload


# ---------------------------------------------------------------- 回滚边界


def test_rollback_boundary_restores_committed_rows(db_path):
    batch = _pending_batch(list_batches(db_path))
    payload = _interrupt(batch["id"], "WM-RB-1", "回滚", db_path=db_path)

    residue = list_residues(db_path)[0]
    assert residue["boundary"] == "回滚"
    assert residue["state"] == "待续写"
    assert residue["committed_rows"] == 2
    assert residue["pending_rows"] == 2
    assert residue["marker"] == "残段·回滚"

    sheet = get_batch_sheet(batch["id"], db_path)
    assert sheet["stamp_state"] == "残段"
    assert sheet["chip"] == "残段·回滚"
    assert [s for s, _ in _rows(sheet)] == ["已盖", "已盖", "待盖", "待盖"]
    assert sheet["consistency"] == []

    # 回滚残段期间，行虽已提交，但批次没收成已盖，页面不能报成功。
    assert sheet["status"] == "待盖印"
    assert payload["pending_rows"] == 2

    result = resume_residue(residue["id"], db_path)
    assert result["boundary"] == "回滚"
    assert result["residue"]["state"] == "已收口"
    assert result["residue"]["rolled_back_rows"] == 2

    sheet = get_batch_sheet(batch["id"], db_path)
    assert sheet["stamp_state"] == "未盖"
    assert sheet["status"] == "待盖印"
    assert sheet["watermark_value"] is None
    assert [s for s, _ in _rows(sheet)] == ["待盖"] * 4
    assert sheet["consistency"] == []


def test_rollback_residue_no_longer_occupies_stock(db_path):
    """回滚边界：残段行退回缺口，库存不被占用。"""

    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-RB-2", "回滚", db_path=db_path)
    before = run_gap_report("2026-11-16", db_path)

    residue = list_residues(db_path)[0]
    assert "残段·回滚" in before["summary"]
    assert before["residue_count"] == 1
    stamped_before = before["stamped_rows"]

    resume_residue(residue["id"], db_path)
    after = run_gap_report("2026-11-16", db_path)
    assert after["stamped_rows"] == stamped_before - 2
    assert after["gap_rows"] == before["gap_rows"] + 2
    assert "已收口·回滚" in after["summary"]


# ---------------------------------------------------------------- 留存边界


def test_retain_boundary_keeps_rows_and_opens_makeup_task(db_path):
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-RT-1", "留存", db_path=db_path)

    residue = list_residues(db_path)[0]
    assert residue["boundary"] == "留存"
    assert residue["marker"] == "残段·留存"

    sheet = get_batch_sheet(batch["id"], db_path)
    assert sheet["chip"] == "残段·留存"
    assert sheet["watermark_value"] == "WM-RT-1"
    assert sheet["consistency"] == []

    result = resume_residue(residue["id"], db_path)
    assert result["boundary"] == "留存"
    assert result["residue"]["state"] == "已收口"
    assert result["batch"]["stamp_state"] == "待补盖"
    task = result["makeup_task"]
    assert task["status"] == "待补盖"
    assert task["remaining_rows"] == 2
    assert len(task["remaining_codes"]) == 2

    sheet = get_batch_sheet(batch["id"], db_path)
    assert [s for s, _ in _rows(sheet)] == ["已盖", "已盖", "待盖", "待盖"]
    assert sheet["consistency"] == []

    filled = fill_makeup_task(task["id"], None, db_path)
    assert filled["filled_rows"] == 2
    assert filled["remaining_rows"] == 0
    assert filled["batch"]["status"] == "已盖印"
    assert filled["batch"]["stamp_state"] == "已盖"
    assert [s for s, _ in _rows(get_batch_sheet(batch["id"], db_path))] == ["已盖"] * 4


def test_retain_residue_occupies_stock_until_makeup(db_path):
    """留存边界：已提交行占库存，缺口比回滚小；补盖后才补平。"""

    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-RT-2", "留存", db_path=db_path)
    residue = list_residues(db_path)[0]
    baseline = run_gap_report("2026-11-16", db_path)

    resume_residue(residue["id"], db_path)
    after_resume = run_gap_report("2026-11-16", db_path)
    assert after_resume["stamped_rows"] == baseline["stamped_rows"]
    assert after_resume["retained_rows"] == 2
    assert "留存 2 行仍占库存" in after_resume["summary"]
    assert "补盖·待补盖" in {b["chip"] for b in list_batches(db_path)}

    task = list_makeup_tasks(db_path)[0]
    fill_makeup_task(task["id"], None, db_path)
    final = run_gap_report("2026-11-16", db_path)
    assert final["stamped_rows"] == after_resume["stamped_rows"] + 2
    assert final["gap_rows"] == after_resume["gap_rows"] - 2


# ---------------------------------------------------------------- 同源拦截


def test_drift_between_rows_and_batch_state_blocks_writes(db_path):
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-DR-1", "回滚", db_path=db_path)

    with connect(db_path) as raw:
        raw.execute("UPDATE run_batches SET stamp_state='已盖' WHERE id=?", (batch["id"],))
        raw.commit()

    sheet = get_batch_sheet(batch["id"], db_path)
    assert sheet["consistency"], "漂移必须能被附页看见"
    assert any("批次态" in issue for issue in sheet["consistency"])

    residue = list_residues(db_path)[0]
    with pytest.raises(ServiceError) as exc:
        resume_residue(residue["id"], db_path)
    assert exc.value.status_code == 409
    assert "漂移" in exc.value.message

    other = next(
        b for b in list_batches(db_path) if b["id"] != batch["id"] and b["status"] == "待盖印"
    )
    with pytest.raises(ServiceError) as exc:
        run_gap_report("2026-11-16", db_path)
    assert exc.value.status_code == 409
    assert exc.value.payload["blocked"] is True

    # 未被漂移影响的批次仍可正常盖印。
    fresh = stamp_batch(other["id"], "WM-DR-OK", db_path)
    assert fresh["status"] == "已盖印"


def test_makeup_codes_must_match_pending_rows(db_path):
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-DR-2", "留存", db_path=db_path)
    residue = list_residues(db_path)[0]
    resume_residue(residue["id"], db_path)
    task = list_makeup_tasks(db_path)[0]

    with connect(db_path) as raw:
        raw.execute(
            "UPDATE stamp_rows SET row_status='已盖', watermark_value='X' "
            "WHERE batch_id=? AND row_no=4",
            (batch["id"],),
        )
        raw.commit()

    with pytest.raises(ServiceError) as exc:
        fill_makeup_task(task["id"], None, db_path)
    assert exc.value.status_code == 409


def test_stamp_refused_while_open_residue(db_path):
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-DR-3", "回滚", db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        stamp_batch(batch["id"], "WM-AGAIN", db_path)
    assert exc.value.status_code == 409
    assert "续写" in exc.value.message


# ---------------------------------------------------------------- 并发续写


def test_concurrent_resume_writes_residue_once(tmp_path):
    """双侧值班并发点续写：只能落一笔，不能双写残段行。"""

    db_path = str(tmp_path / "race.db")
    from app.db import init_db

    init_db(db_path)
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-RACE", "留存", db_path=db_path)
    residue = list_residues(db_path)[0]

    # 用独立连接并发，围绕「读到待续写 → 写收口」这一段制造竞争窗口。
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def worker():
        barrier.wait()
        try:
            resume_residue(residue["id"], db_path)
            outcomes.append(("ok", ""))
        except ServiceError as exc:
            outcomes.append(("err", exc.message))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(o[0] for o in outcomes) == ["err", "ok"], outcomes
    assert "已收口" in next(m for s, m in outcomes if s == "err")

    with sqlite3.connect(db_path) as raw:
        task_count = raw.execute(
            "SELECT COUNT(*) FROM makeup_tasks WHERE residue_id=?", (residue["id"],)
        ).fetchone()[0]
        closed = raw.execute(
            "SELECT COUNT(*) FROM residue_segments WHERE id=? AND state='已收口'",
            (residue["id"],),
        ).fetchone()[0]
        stamped = raw.execute(
            "SELECT COUNT(*) FROM stamp_rows WHERE batch_id=? AND row_status='已盖'",
            (batch["id"],),
        ).fetchone()[0]
    assert task_count == 1
    assert closed == 1
    assert stamped == 2, "续写只应承接已提交的 2 行，不能被双写"
    assert get_batch_sheet(batch["id"], db_path)["consistency"] == []


def test_resume_serialises_behind_write_lock(tmp_path):
    """续写持有 BEGIN IMMEDIATE 写锁：另一侧写事务必须排队而不是插队双写。"""

    db_path = str(tmp_path / "lock.db")
    from app.db import init_db, immediate_transaction

    init_db(db_path)
    batch = _pending_batch(list_batches(db_path))
    _interrupt(batch["id"], "WM-LOCK", "回滚", db_path=db_path)
    residue = list_residues(db_path)[0]

    held = threading.Event()
    release = threading.Event()

    def holder():
        with immediate_transaction(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)

    result: dict = {}

    def resumer():
        try:
            resume_residue(residue["id"], db_path)
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - failure path asserted below
            result["error"] = exc

    worker = threading.Thread(target=resumer)
    worker.start()
    worker.join(0.3)
    assert worker.is_alive(), "写锁被占时续写必须阻塞排队"
    release.set()
    thread.join(5)
    worker.join(5)
    assert result.get("ok") is True

    with sqlite3.connect(db_path) as raw:
        row = raw.execute(
            "SELECT state, rolled_back_rows FROM residue_segments WHERE id=?", (residue["id"],)
        ).fetchone()
    assert row[0] == "已收口"
    assert row[1] == 2
