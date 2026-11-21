"""跨档迁移：路径 A（同事务释源插目标）。

正确性口径当日锁死，见 docs/转移双改说明.md：
- 只走 A：一笔事务内释源 + 占目标；任一步失败两边同滚，源仍占、目标零增。
- 禁止路径 B（转移影子/先刷目标档展示）与任何混用。
- 偏航即记 audit transfer_double_write_deviation 并拒写（409），业务两边零改。
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, try_acquire

router = APIRouter(tags=["transfer"])

# 当日锁死的正确性口径；只允许这一个值。
TRANSFER_WRITE_PATH = "A_same_transaction_double_write"
TRANSFER_PATH_ENV = "SLOTBOOK_TRANSFER_PATH"
_DEV_ACTION = "transfer_double_write_deviation"

# 混用检测：路径 B 的影子表一旦出现即判偏航。
_FORBIDDEN_SHADOW_TABLES = ("transfer_shadow",)


class TransferIn(BaseModel):
    booking_id: int
    to_slot_code: str
    note: str = ""


class TransferRefused(Exception):
    """业务拒绝：需要整笔回滚并保留可读原因。"""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@router.get("/api/transfer/jobs")
def list_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transfer_jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


def _detect_deviation(conn) -> str | None:
    requested = os.environ.get(TRANSFER_PATH_ENV, TRANSFER_WRITE_PATH)
    if requested != TRANSFER_WRITE_PATH:
        return f"口径被改为 {requested}，本产品只允许 {TRANSFER_WRITE_PATH}"
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
        % ",".join("?" * len(_FORBIDDEN_SHADOW_TABLES)),
        _FORBIDDEN_SHADOW_TABLES,
    ).fetchall()
    if rows:
        names = ",".join(r["name"] for r in rows)
        return f"检测到路径 B 影子表 {names}，与路径 A 混用"
    return None


def _refuse_deviation(conn, user: str, body: TransferIn, reason: str) -> None:
    write_audit(
        conn,
        user,
        _DEV_ACTION,
        str(body.booking_id),
        {
            "to": body.to_slot_code,
            "requested_path": os.environ.get(TRANSFER_PATH_ENV, TRANSFER_WRITE_PATH),
            "locked_path": TRANSFER_WRITE_PATH,
            "reason": reason,
        },
    )
    conn.commit()
    raise HTTPException(409, f"转移双改偏航，拒绝写入：{reason}")


def _acquire_all(conn, keys: list[str], user: str) -> list[str]:
    """按固定顺序抢锁；任一争用则只留争用痕并交还本次已抢的锁。"""
    acquired: list[str] = []
    for key in sorted(keys):
        if not try_acquire(conn, key, user):
            for held in acquired:
                release(conn, held, user)
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用痕，不写业务")
        acquired.append(key)
    conn.commit()
    return acquired


@router.post("/api/transfer")
def transfer_booking(body: TransferIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        deviation = _detect_deviation(conn)
        if deviation:
            _refuse_deviation(conn, user, body, deviation)

        b = conn.execute(
            "SELECT * FROM bookings WHERE id=?", (body.booking_id,)
        ).fetchone()
        if not b:
            raise HTTPException(404, "正式预订不存在")
        src = conn.execute("SELECT * FROM slots WHERE id=?", (b["slot_id"],)).fetchone()
        dst = conn.execute(
            "SELECT * FROM slots WHERE code=?", (body.to_slot_code,)
        ).fetchone()
        if not dst:
            raise HTTPException(404, "目标档期不存在")
        if src is None:
            raise HTTPException(409, "源档期不存在，无法释源")
        if src["id"] == dst["id"]:
            raise HTTPException(409, "源档与目标档相同，无需转移")

        held = _acquire_all(
            conn,
            [f"booking:{b['id']}", f"slot:{src['code']}", f"slot:{dst['code']}"],
            user,
        )
        try:
            # 重读，避免抢锁前的过期视图
            b = conn.execute(
                "SELECT * FROM bookings WHERE id=?", (body.booking_id,)
            ).fetchone()
            src = conn.execute(
                "SELECT * FROM slots WHERE id=?", (b["slot_id"],)
            ).fetchone()
            dst = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.to_slot_code,)
            ).fetchone()
            if b["status"] != "已确认":
                raise TransferRefused("正式册非已确认，禁止转移")
            if dst["status"] != "开放":
                raise TransferRefused("目标档未开放，禁止转移")
            seats = int(b["seats"])
            if seats <= 0:
                raise TransferRefused("席位非法，禁止转移")

            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 源释：盘面不足则整笔拒绝，宁可不动也不半截
                cur_src = conn.execute(
                    "UPDATE slots SET booked = booked - ? WHERE id=? AND booked >= ?",
                    (seats, src["id"], seats),
                )
                if cur_src.rowcount != 1:
                    raise TransferRefused("源档盘面不足以释放该席位，整笔回滚")

                # 目标占：余席须扣掉目标档占位中 holds
                cur_dst = conn.execute(
                    """UPDATE slots SET booked = booked + ?
                       WHERE id=? AND status='开放'
                         AND booked + ? + (
                           SELECT COALESCE(SUM(seats),0) FROM holds
                           WHERE slot_id=? AND status='占位中'
                         ) <= capacity""",
                    (seats, dst["id"], seats, dst["id"]),
                )
                if cur_dst.rowcount != 1:
                    raise TransferRefused("目标档余席不足，整笔回滚")

                cur_book = conn.execute(
                    """UPDATE bookings SET slot_id=?, snapshot_gen=?
                       WHERE id=? AND status='已确认' AND slot_id=?""",
                    (dst["id"], dst["generation"], b["id"], src["id"]),
                )
                if cur_book.rowcount != 1:
                    raise TransferRefused("正式册已不在源档，判重复转移，整笔回滚")

                # 提交前复核释源已生效，防只刷目标档
                check = conn.execute(
                    """SELECT
                         (SELECT booked FROM slots WHERE id=?) AS src_booked,
                         (SELECT COUNT(*) FROM bookings
                          WHERE id=? AND slot_id=?) AS still_in_src""",
                    (src["id"], b["id"], src["id"]),
                ).fetchone()
                if int(check["src_booked"]) != int(src["booked"]) - seats:
                    raise TransferRefused("源档未松开，判半截写入，整笔回滚")
                if int(check["still_in_src"]) != 0:
                    raise TransferRefused("正式册仍挂源档，判半截写入，整笔回滚")

                cur = conn.execute(
                    """INSERT INTO transfer_jobs(booking_id,to_slot_code,status,note,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        b["id"],
                        body.to_slot_code,
                        "已转移",
                        (body.note or "同事务释源插目标") + "（路径 A）",
                        now,
                    ),
                )

                new_src = conn.execute(
                    "SELECT booked FROM slots WHERE id=?", (src["id"],)
                ).fetchone()
                new_dst = conn.execute(
                    "SELECT booked FROM slots WHERE id=?", (dst["id"],)
                ).fetchone()
                write_audit(
                    conn,
                    user,
                    "booking_transfer",
                    b["party_key"],
                    {
                        "booking_id": b["id"],
                        "from_slot": src["code"],
                        "to_slot": dst["code"],
                        "seats": seats,
                        "path": TRANSFER_WRITE_PATH,
                        "job_id": cur.lastrowid,
                        "src_booked": [int(src["booked"]), int(new_src["booked"])],
                        "dst_booked": [int(dst["booked"]), int(new_dst["booked"])],
                    },
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        except TransferRefused as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        finally:
            for key in held:
                release(conn, key, user)
            conn.commit()

        return {
            "ok": True,
            "stub": False,
            "path": TRANSFER_WRITE_PATH,
            "source_released": True,
            "job_id": cur.lastrowid,
            "booking_id": b["id"],
            "from_slot_code": src["code"],
            "to_slot_code": body.to_slot_code,
            "seats": seats,
        }
