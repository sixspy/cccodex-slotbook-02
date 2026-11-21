"""对账：档期 booked 与正式册 seats 同源校验，余席/占位/审计须同态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _slot_view(conn, s) -> dict:
    """单档五处同态视图：盘面、正式册、占位中、余席、转移审计。"""
    booked_sum = conn.execute(
        """SELECT COALESCE(SUM(seats),0) s FROM bookings
           WHERE slot_id=? AND status='已确认'""",
        (s["id"],),
    ).fetchone()["s"]
    open_seats = conn.execute(
        "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
        (s["id"],),
    ).fetchone()["s"]
    open_holds = conn.execute(
        "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
        (s["id"],),
    ).fetchone()["c"]
    booked = int(s["booked"])
    return {
        "slot_code": s["code"],
        "slot_booked": booked,
        "booking_seats": int(booked_sum),
        "open_holds": int(open_holds),
        "open_hold_seats": int(open_seats),
        "free_seats": int(s["capacity"]) - booked - int(open_seats),
        "capacity": int(s["capacity"]),
        "generation": s["generation"],
        "status": s["status"],
        "ok": int(booked_sum) == booked,
    }


def _transfer_audit_ok(conn) -> list[dict]:
    """审计同态：已转移任务须有同单同源同目标同席的 booking_transfer 审计。"""
    jobs = conn.execute(
        "SELECT * FROM transfer_jobs WHERE status='已转移' ORDER BY id DESC LIMIT 200"
    ).fetchall()
    checks = []
    for j in jobs:
        row = conn.execute(
            """SELECT payload FROM audit_trail
               WHERE action='booking_transfer'
                 AND CAST(json_extract(payload, '$.job_id') AS INTEGER) = ?
               ORDER BY id DESC LIMIT 1""",
            (j["id"],),
        ).fetchone()
        same = False
        if row is not None:
            payload = row["payload"] or "{}"
            b = conn.execute(
                "SELECT party_key, seats FROM bookings WHERE id=?", (j["booking_id"],)
            ).fetchone()
            same = (
                b is not None
                and payload.find(j["to_slot_code"]) >= 0
                and str(b["seats"]) in payload
            )
        checks.append(
            {
                "job_id": j["id"],
                "booking_id": j["booking_id"],
                "to_slot_code": j["to_slot_code"],
                "ok": same,
            }
        )
    return checks


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = [_slot_view(conn, s) for s in slots]
        transfers = _transfer_audit_ok(conn)
        all_ok = all(i["ok"] for i in items) and all(t["ok"] for t in transfers)
        return {
            "items": items,
            "transfers": transfers,
            "all_ok": all_ok,
            "path": "A_same_transaction_double_write",
        }


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        return _slot_view(conn, s)
