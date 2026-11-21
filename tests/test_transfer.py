"""跨档转移：正确性口径 = 路径 A（同事务释源插目标），五处同态。

先见红记录（2026-11-21）：
- test_transfer_releases_source_occupies_target_five_way
- test_two_people_same_booking_later_only_contention
两测在空桩/半截写入下必须失败，改服务后才可转绿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
import db as dbmod  # noqa: E402
from db import init_db  # noqa: E402
from transfer_fixtures import (  # noqa: E402
    apply_false_green_transfer,
    five_way_errors,
    free_seats,
    snapshot,
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_booking(c, h, seats=3, party="P-XFER", slot="S-AM-01"):
    r = c.post(
        "/api/holds",
        headers=h,
        json={"slot_code": slot, "party_key": party, "seats": seats},
    )
    assert r.status_code == 200, r.text
    assert r.json()["disposition"] == "占位", r.text
    hid = r.json()["hold_id"]
    r2 = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
    assert r2.status_code == 200, r2.text
    return r2.json()["booking_id"]


def test_false_green_fixture_is_detectable():
    """假绿夹具：转移显示成功但源档仍占，对账必须识破。"""
    with TestClient(app) as c:
        h = _login(c)
        bid = _seed_booking(c, h, seats=3, party="P-FG", slot="S-AM-01")
        before = snapshot(c, h)
        res = apply_false_green_transfer(bid, "S-PM-02")
        assert res["ok"] is True
        after = snapshot(c, h)
        errs = five_way_errors(
            before,
            after,
            booking_id=bid,
            from_code="S-AM-01",
            to_code="S-PM-02",
            seats=3,
        )
        assert errs, "假绿夹具应被五处同态识破"
        assert any("源占位未释" in e for e in errs)
        assert after["reconcile"]["all_ok"] is False
        # 目标档虚高一席、源档没松开
        assert int(after["slots"]["S-PM-02"]["booked"]) == int(
            before["slots"]["S-PM-02"]["booked"]
        ) + 3
        assert int(after["slots"]["S-AM-01"]["booked"]) == int(
            before["slots"]["S-AM-01"]["booked"]
        )


def test_transfer_releases_source_occupies_target_five_way():
    """转移成功 => 源释、目标占、余席、对账、审计五处同态。"""
    with TestClient(app) as c:
        h = _login(c)
        bid = _seed_booking(c, h, seats=3, party="P-MOVE", slot="S-AM-01")
        before = snapshot(c, h)
        r = c.post(
            "/api/transfer",
            headers=h,
            json={"booking_id": bid, "to_slot_code": "S-PM-02"},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True, r.text
        assert r.json().get("path") == "A_same_transaction_double_write"
        assert r.json().get("source_released") is True
        after = snapshot(c, h)
        errs = five_way_errors(
            before,
            after,
            booking_id=bid,
            from_code="S-AM-01",
            to_code="S-PM-02",
            seats=3,
        )
        assert not errs, "五处不同态：" + "；".join(errs)


def test_transfer_rejects_when_target_short_of_free_seats_same_rollback():
    """目标容量不足 => 两边零改，源仍占。"""
    with TestClient(app) as c:
        h = _login(c)
        # S-EVE-03 容量 6，先占满 5 席留 1
        h1 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-EVE-03", "party_key": "P-FILL", "seats": 5},
        ).json()["hold_id"]
        assert (
            c.post("/api/bookings/confirm", headers=h, json={"hold_id": h1}).status_code
            == 200
        )
        bid = _seed_booking(c, h, seats=3, party="P-BIG", slot="S-AM-01")
        before = snapshot(c, h)
        r = c.post(
            "/api/transfer",
            headers=h,
            json={"booking_id": bid, "to_slot_code": "S-EVE-03"},
        )
        assert r.status_code == 409, r.text
        after = snapshot(c, h)
        assert after["slots"] == before["slots"]
        assert after["bookings"] == before["bookings"]
        assert after["reconcile"]["all_ok"] is True
        assert after["jobs"] == before["jobs"]


def test_two_people_same_booking_later_only_contention():
    """两人同挪同一源单：先到成功，后到只留争用痕、不写业务。"""
    with TestClient(app) as c:
        ha = _login(c, "admin")
        hb = _login(c, "clerk")
        bid = _seed_booking(c, ha, seats=2, party="P-RACE", slot="S-AM-01")
        before = snapshot(c, ha)

        # 先到者已持档锁（同源同单），后到者抢锁必争用
        r_lock = c.post(
            "/api/locks/acquire", headers=ha, json={"lock_key": f"booking:{bid}"}
        )
        assert r_lock.status_code == 200, r_lock.text

        r_late = c.post(
            "/api/transfer",
            headers=hb,
            json={"booking_id": bid, "to_slot_code": "S-PM-02"},
        )
        assert r_late.status_code == 409, r_late.text
        assert "争用" in r_late.text

        # 后到不写业务：盘面与正式册零改
        mid = snapshot(c, ha)
        assert mid["slots"] == before["slots"]
        assert mid["bookings"] == before["bookings"]
        assert mid["jobs"] == before["jobs"]

        # 争用只留痕
        cont = c.get("/api/contention", headers=ha).json()["items"]
        assert any(
            x["lock_key"] == f"booking:{bid}" and x["actor"] == "clerk" for x in cont
        ), cont

        # 先到者释放锁后可完成转移，后到仍不写
        c.post("/api/locks/release", headers=ha, json={"lock_key": f"booking:{bid}"})
        r_win = c.post(
            "/api/transfer",
            headers=ha,
            json={"booking_id": bid, "to_slot_code": "S-PM-02"},
        )
        assert r_win.status_code == 200, r_win.text
        after = snapshot(c, ha)
        errs = five_way_errors(
            before,
            after,
            booking_id=bid,
            from_code="S-AM-01",
            to_code="S-PM-02",
            seats=2,
        )
        assert not errs, "；".join(errs)


def test_transfer_wrong_path_is_deviation_and_refused(monkeypatch):
    """偏航：口径被改成路径 B => 记转移双改偏航并拒写，两边零改。"""
    from transfer_svc import TRANSFER_PATH_ENV

    with TestClient(app) as c:
        h = _login(c)
        bid = _seed_booking(c, h, seats=2, party="P-DEV", slot="S-AM-01")
        before = snapshot(c, h)
        monkeypatch.setenv(TRANSFER_PATH_ENV, "B_shadow_switch")
        r = c.post(
            "/api/transfer",
            headers=h,
            json={"booking_id": bid, "to_slot_code": "S-PM-02"},
        )
        assert r.status_code == 409, r.text
        assert "偏航" in r.text
        after = snapshot(c, h)
        assert after["slots"] == before["slots"]
        assert after["bookings"] == before["bookings"]
        devs = [
            a for a in after["audit"] if a["action"] == "transfer_double_write_deviation"
        ]
        assert devs, "偏航必须落审计"
        assert "B_shadow_switch" in (devs[0]["payload"] or "")
