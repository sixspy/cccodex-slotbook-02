"""转移测试夹具：复现“转移显示成功、源档仍占”的假绿盘面。

只用本机文件库（测试临时库文件），不碰产品库 data/app.db。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import db as dbmod


def open_fixture_db() -> sqlite3.Connection:
    conn = sqlite3.connect(dbmod.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def apply_false_green_transfer(
    booking_id: int, to_slot_code: str, actor: str = "fixture"
) -> dict:
    """半截写入：只把目标档盘面加上、正式册挪过去，源档 booked 故意不扣。

    返回形似成功的响应；这正是“目标档多一席、源档没松开”的假绿。
    """
    with open_fixture_db() as conn:
        b = conn.execute(
            "SELECT * FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()
        if not b:
            raise LookupError("正式预订不存在")
        dst = conn.execute(
            "SELECT * FROM slots WHERE code=?", (to_slot_code,)
        ).fetchone()
        if not dst:
            raise LookupError("目标档期不存在")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE slots SET booked = booked + ? WHERE id=?",
            (b["seats"], dst["id"]),
        )
        conn.execute(
            "UPDATE bookings SET slot_id=?, snapshot_gen=? WHERE id=?",
            (dst["id"], dst["generation"], b["id"]),
        )
        cur = conn.execute(
            """INSERT INTO transfer_jobs(booking_id,to_slot_code,status,note,created_at)
               VALUES (?,?,?,?,?)""",
            (b["id"], to_slot_code, "已转移", "假绿夹具：源档未释放", now),
        )
        dbmod.write_audit(
            conn,
            actor,
            "booking_transfer",
            b["party_key"],
            {
                "booking_id": b["id"],
                "from_slot_id": b["slot_id"],
                "to_slot_code": to_slot_code,
                "seats": b["seats"],
                "path": "false_green_fixture",
            },
        )
        conn.commit()
        return {
            "ok": True,
            "job_id": cur.lastrowid,
            "booking_id": b["id"],
            "to_slot_code": to_slot_code,
            "seats": b["seats"],
        }


def snapshot(client, headers) -> dict:
    """抓五处盘面：档期、正式册、占位、对账、审计。"""
    slots = {
        s["code"]: s for s in client.get("/api/slots", headers=headers).json()["items"]
    }
    holds = client.get("/api/holds", headers=headers).json()["items"]
    open_holds: dict[str, int] = {}
    for h in holds:
        if h["status"] == "占位中":
            open_holds[h["slot_code"]] = open_holds.get(h["slot_code"], 0) + int(
                h["seats"]
            )
    return {
        "slots": slots,
        "open_holds": open_holds,
        "bookings": client.get("/api/bookings", headers=headers).json()["items"],
        "reconcile": client.get("/api/reconcile", headers=headers).json(),
        "audit": client.get("/api/audit", headers=headers, params={"limit": 500})
        .json()["items"],
        "jobs": client.get("/api/transfer/jobs", headers=headers).json()["items"],
    }


def free_seats(snap: dict, code: str) -> int:
    s = snap["slots"][code]
    return int(s["capacity"]) - int(s["booked"]) - int(snap["open_holds"].get(code, 0))


def occupied_seats(snap: dict, code: str) -> int:
    return int(snap["slots"][code]["booked"]) + int(snap["open_holds"].get(code, 0))


def five_way_errors(
    before: dict,
    after: dict,
    *,
    booking_id: int,
    from_code: str,
    to_code: str,
    seats: int,
) -> list[str]:
    """路径 A 的五处同态断言，返回所有不成立项。"""
    errs: list[str] = []
    src_b, dst_b = before["slots"][from_code], before["slots"][to_code]
    src_a, dst_a = after["slots"][from_code], after["slots"][to_code]

    # 1 源占位：源档 booked 必须松开
    if int(src_a["booked"]) != int(src_b["booked"]) - seats:
        errs.append(
            f"源占位未释：{from_code} booked {src_b['booked']}->{src_a['booked']} 应减 {seats}"
        )
    # 2 目标占位：目标档 booked 必须增加
    if int(dst_a["booked"]) != int(dst_b["booked"]) + seats:
        errs.append(
            f"目标占位未增：{to_code} booked {dst_b['booked']}->{dst_a['booked']} 应加 {seats}"
        )
    # 3 余席：源增目标减，总占位不虚高
    if free_seats(after, from_code) != free_seats(before, from_code) + seats:
        errs.append("源档余席未按席位增加")
    if free_seats(after, to_code) != free_seats(before, to_code) - seats:
        errs.append("目标档余席未按席位减少")
    total_b = occupied_seats(before, from_code) + occupied_seats(before, to_code)
    total_a = occupied_seats(after, from_code) + occupied_seats(after, to_code)
    if total_a != total_b:
        errs.append(f"总占位虚高：{total_b}->{total_a}")

    # 正式册归属
    moved = [b for b in after["bookings"] if b["id"] == booking_id]
    if not moved:
        errs.append("正式册丢失该单")
    else:
        got = moved[0]["slot_code"]
        if got != to_code:
            errs.append(f"正式册仍挂 {got}，应为 {to_code}")
        if int(moved[0]["seats"]) != seats:
            errs.append("正式册席位数被改动")
    still_src = [
        b
        for b in after["bookings"]
        if b["slot_code"] == from_code and b["id"] == booking_id
    ]
    if still_src:
        errs.append("源档正式册仍含该单")

    # 4 对账
    if after["reconcile"].get("all_ok") is not True:
        bad = [
            i
            for i in after["reconcile"].get("items", [])
            if not i.get("ok")
        ]
        errs.append(f"对账分裂：{bad}")
    for code in (from_code, to_code):
        item = next(
            (
                i
                for i in after["reconcile"].get("items", [])
                if i["slot_code"] == code
            ),
            None,
        )
        if not item or item.get("ok") is not True:
            errs.append(f"对账未通过：{code}")

    # 5 审计与转移任务同源
    party = moved[0]["party_key"] if moved else None
    audits = [
        a
        for a in after["audit"]
        if moved and a["action"] == "booking_transfer" and a["target"] == party
    ]
    if not audits:
        errs.append("审计缺 booking_transfer")
    else:
        payload = audits[0]["payload"] or ""
        if to_code not in payload or from_code not in payload:
            errs.append("审计未同时记录源档与目标档")
    jobs = [j for j in after["jobs"] if j["booking_id"] == booking_id]
    if not jobs or jobs[0]["status"] != "已转移":
        errs.append("转移任务未落已转移")
    elif jobs[0]["to_slot_code"] != to_code:
        errs.append("转移任务目标档不一致")
    return errs
