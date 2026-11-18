import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "血缘水印台"
    SECRET = os.environ.get("SECRET_KEY", "cc-watermark-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "watermark_lab.db")
    # 残段提交边界的默认选边：回滚（整笔回滚已提交行，残段不占库存）
    RESIDUE_BOUNDARY = os.environ.get("RESIDUE_BOUNDARY", "回滚")

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)
