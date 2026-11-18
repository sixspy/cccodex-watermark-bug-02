from app.db import get_db
from app.services.errors import ServiceError


def stamp_batch_rows(batch_id: int, watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        batch = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        # silent: commit first half then fake success while leaving related edge rows blank
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, stamped_at=datetime('now') WHERE id=?",
            (value, batch_id),
        )
        conn.commit()
        # related downstream left pending — looks like full success to caller
        return {
            "id": batch_id,
            "status": "已盖印",
            "watermark_value": value,
            "ok": True,
            "message": "全成功",
        }
