"""接口层验收：中断不报全成功、残段目录 / 附页 / 对账席同源、并发续写排他。"""

import pytest


async def _pending(client):
    items = (await (await client.get("/api/batches")).json())["items"]
    return [b for b in items if b["status"] == "待盖印"]


@pytest.mark.asyncio
async def test_interrupt_reports_failure_not_success(authed):
    batch = (await _pending(authed))[0]
    r = await authed.post(
        f"/api/batches/{batch['id']}/stamp",
        json={"watermark_value": "WM-API-1", "boundary": "回滚", "fail_after": 2},
    )
    assert r.status == 409
    body = await r.json()
    assert body["ok"] is False
    assert "全成功" not in body["error"]
    assert body["boundary"] == "回滚"
    assert body["committed_rows"] == 2
    assert body["pending_rows"] == 2

    residues = (await (await authed.get("/api/residues")).json())["items"]
    assert len(residues) == 1
    assert residues[0]["boundary"] == "回滚"
    assert residues[0]["state"] == "待续写"
    assert residues[0]["chip"] == "残段·回滚"

    sheet = (await (await authed.get(f"/api/batches/{batch['id']}/sheet")).json())["sheet"]
    assert sheet["stamp_state"] == "残段"
    assert sheet["chip"] == "残段·回滚"
    assert [r["row_status"] for r in sheet["rows"]] == ["已盖", "已盖", "待盖", "待盖"]
    assert sheet["consistency"] == []

    listed = (await (await authed.get("/api/batches")).json())["items"]
    row = next(b for b in listed if b["id"] == batch["id"])
    assert row["chip"] == "残段·回滚"
    assert row["status"] == "待盖印"

    # 对账席必须露出同一标记。
    gap = (await (await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})).json())
    assert "残段·回滚" in gap["report"]["summary"]
    assert gap["report"]["residue_count"] == 1


@pytest.mark.asyncio
async def test_rollback_resume_via_api(authed):
    batch = (await _pending(authed))[0]
    await authed.post(
        f"/api/batches/{batch['id']}/stamp",
        json={"watermark_value": "WM-API-2", "boundary": "回滚", "fail_after": 1},
    )
    residue = (await (await authed.get("/api/residues")).json())["items"][0]
    r = await authed.post(f"/api/residues/{residue['id']}/resume")
    assert r.status == 200
    body = await r.json()
    assert body["boundary"] == "回滚"
    assert body["batch"]["stamp_state"] == "未盖"
    assert body["residue"]["state"] == "已收口"

    again = await authed.post(f"/api/residues/{residue['id']}/resume")
    assert again.status == 409

    sheet = (await (await authed.get(f"/api/batches/{batch['id']}/sheet")).json())["sheet"]
    assert [r["row_status"] for r in sheet["rows"]] == ["待盖"] * 4

    gap = (await (await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})).json())
    assert "已收口·回滚" in gap["report"]["summary"]


@pytest.mark.asyncio
async def test_retain_resume_and_makeup_via_api(authed):
    batch = (await _pending(authed))[0]
    await authed.post(
        f"/api/batches/{batch['id']}/stamp",
        json={"watermark_value": "WM-API-3", "boundary": "留存", "fail_after": 2},
    )
    residue = (await (await authed.get("/api/residues")).json())["items"][0]
    resumed = (await (await authed.post(f"/api/residues/{residue['id']}/resume")).json())
    assert resumed["batch"]["stamp_state"] == "待补盖"
    task = resumed["makeup_task"]
    assert task["status"] == "待补盖"
    assert task["remaining_rows"] == 2

    listed = (await (await authed.get("/api/batches")).json())["items"]
    row = next(b for b in listed if b["id"] == batch["id"])
    assert row["chip"] == "补盖·待补盖"

    filled = (await (await authed.post(f"/api/makeup-tasks/{task['id']}/fill", json={})).json())
    assert filled["batch"]["status"] == "已盖印"
    assert filled["remaining_rows"] == 0

    sheet = (await (await authed.get(f"/api/batches/{batch['id']}/sheet")).json())["sheet"]
    assert [r["row_status"] for r in sheet["rows"]] == ["已盖"] * 4
    assert sheet["consistency"] == []


@pytest.mark.asyncio
async def test_consistency_endpoint_surfaces_drift(authed):
    batch = (await _pending(authed))[0]
    await authed.post(
        f"/api/batches/{batch['id']}/stamp",
        json={"watermark_value": "WM-API-4", "boundary": "回滚", "fail_after": 2},
    )
    ok = await (await authed.get("/api/consistency")).json()
    assert ok["ok"] is True

    import sqlite3

    from app.config import Config

    with sqlite3.connect(Config.database()) as raw:
        raw.execute("UPDATE run_batches SET stamp_state='已盖' WHERE id=?", (batch["id"],))
        raw.commit()

    drift = await (await authed.get("/api/consistency")).json()
    assert drift["ok"] is False
    assert batch["run_code"] in drift["issues"]

    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    assert gap.status == 409
    assert (await gap.json())["blocked"] is True
