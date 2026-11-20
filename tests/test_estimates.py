"""估读（暂估读数）规则测试。

覆盖：
* 估读上传沿用读数 CSV，采纳状态/未采纳原因可查；
* 实抄照原规则入库，同刻估读让位实抄，值不同也不阻拦实抄；
* 相邻实抄之间、无换表、插入全部估读不倒退才整段采纳；
* 补传实抄/估读/换表只增量重核受影响分段，失败回滚保留原数据与状态；
* 用量页与 API 默认实抄口径，可查暂估口径并标记以估读为端点的增量日期。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app import logic  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, kind, csv_text, filename="data.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post(
        f"/upload/{kind}", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def usage(client, point, start, end, basis=None):
    url = f"/api/usage?point={point}&start={start}&end={end}"
    if basis:
        url += f"&basis={basis}"
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_json()


def day_kwh(result, day):
    return next(d["kwh"] for d in result["days"] if d["day"] == day)


def day_estimated(result, day):
    return next(d["estimated"] for d in result["days"] if d["day"] == day)


def estimate_rows(client, point="A"):
    """直接读库返回 {ts: (status, reason, value)}。"""
    with client.application.app_context():
        from app.db import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT ts, status, reason, value FROM estimates"
            " WHERE point = ? ORDER BY ts",
            (point,),
        ).fetchall()
        return {r["ts"]: (r["status"], r["reason"], r["value"]) for r in rows}


def canon(day="2026-01-02", hhmmss="00:00:00"):
    return f"{day}T{hhmmss}.000000Z"


# ---------------------------------------------------------------- 采纳基本规则

def test_estimates_adopted_between_two_actuals(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    resp = upload(client, "estimates",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
                  "A,2026-01-03T00:00:00Z,140\n")
    assert "采纳 2 条，未采纳 0 条" in resp.get_data(as_text=True)
    rows = estimate_rows(client)
    assert all(v[0] == logic.EST_ADOPTED for v in rows.values())

    # 实抄口径不受估读影响：60 仍整体计入 1.4
    actual = usage(client, "A", "2026-01-01", "2026-01-04")
    assert [d["kwh"] for d in actual["days"]] == ["0", "0", "0", "60"]
    assert all(d["estimated"] is False for d in actual["days"])

    # 暂估口径：60 拆成 20+20+20，且相关日期被标记
    prov = usage(client, "A", "2026-01-01", "2026-01-04", basis="provisional")
    assert [d["kwh"] for d in prov["days"]] == ["0", "20", "20", "20"]
    assert [d["estimated"] for d in prov["days"]] == [False, True, True, True]
    assert prov["total_kwh"] == "60"  # 拆分不改变总量


def test_estimate_outside_actual_span_is_unbounded(client):
    upload(client, "readings", "point,ts,value\nA,2026-01-02T00:00:00Z,100\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-01T00:00:00Z,90\n"   # 首条实抄之前
           "A,2026-01-03T00:00:00Z,110\n")                 # 末条实抄之后
    rows = estimate_rows(client)
    for ts, (status, reason, _) in rows.items():
        assert status == logic.EST_REJECTED
        assert reason == logic.EST_REASON_UNBOUNDED
    # 未采纳估读不参与任何口径
    prov = usage(client, "A", "2026-01-01", "2026-01-03", basis="provisional")
    assert prov["total_kwh"] == "0"


def test_unbounded_estimate_gets_adopted_after_late_actual(client):
    # 先有估读（无界），补传末条实抄后分段成立、不倒退 -> 转为采纳
    upload(client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,100\n")
    upload(client, "estimates", "point,ts,value\nA,2026-01-02T00:00:00Z,120\n")
    rows = estimate_rows(client)
    assert rows[canon()][0] == logic.EST_REJECTED
    assert rows[canon()][1] == logic.EST_REASON_UNBOUNDED

    upload(client, "readings", "point,ts,value\nA,2026-01-03T00:00:00Z,150\n")
    rows = estimate_rows(client)
    assert rows[canon()][0] == logic.EST_ADOPTED
    prov = usage(client, "A", "2026-01-02", "2026-01-03", basis="provisional")
    assert day_kwh(prov, "2026-01-02") == "20"
    assert day_estimated(prov, "2026-01-02") is True


# ---------------------------------------------------------------- 整段同进同退

def test_backwards_estimate_rejects_whole_segment(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    # 120 本可采纳，但同段另一条 200 造成倒退 -> 整段不采纳
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,200\n")
    rows = estimate_rows(client)
    assert all(v[0] == logic.EST_REJECTED for v in rows.values())
    assert all(v[1] == logic.EST_REASON_BACKWARDS for v in rows.values())
    # 暂估口径回退为实抄口径：60 整体在 1.4
    prov = usage(client, "A", "2026-01-01", "2026-01-04", basis="provisional")
    assert [d["kwh"] for d in prov["days"]] == ["0", "0", "0", "60"]
    assert all(d["estimated"] is False for d in prov["days"])


def test_late_backwards_estimate_unadopts_previous_segment(client):
    # 先采纳两条估读，后补一条导致倒退的估读 -> 同段全部改判未采纳
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,140\n")
    assert all(v[0] == logic.EST_ADOPTED for v in estimate_rows(client).values())

    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T12:00:00Z,135\n")  # 135 <=140 仍正常，先采纳
    assert all(v[0] == logic.EST_ADOPTED for v in estimate_rows(client).values())

    upload(client, "estimates",
           "point,ts,value\nA,2026-01-03T12:00:00Z,999\n")  # 999 > 160 倒退
    rows = estimate_rows(client)
    assert all(v[0] == logic.EST_REJECTED for v in rows.values())
    assert all(v[1] == logic.EST_REASON_BACKWARDS for v in rows.values())


def test_swap_makes_segment_estimates_rejected(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,140\n")
    assert all(v[0] == logic.EST_ADOPTED for v in estimate_rows(client).values())

    # 补录换表落在实抄区间 (1.1, 1.4]：整段估读改判，实抄区间同时变缺口
    upload(client, "swaps", "point,ts\nA,2026-01-02T12:00:00Z\n")
    rows = estimate_rows(client)
    assert all(v[0] == logic.EST_REJECTED for v in rows.values())
    assert all(v[1] == logic.EST_REASON_SWAP for v in rows.values())
    prov = usage(client, "A", "2026-01-01", "2026-01-04", basis="provisional")
    assert prov["total_kwh"] == "0"
    assert len(prov["gaps"]) == 1


def test_swap_in_neighboring_segment_only_reevaluates_that_segment(client):
    # 两个相邻实抄分段各有估读；换表只影响落入的那一段
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,130\n"
           "A,2026-01-05T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,115\n"
           "A,2026-01-04T00:00:00Z,145\n")
    assert all(v[0] == logic.EST_ADOPTED for v in estimate_rows(client).values())

    upload(client, "swaps", "point,ts\nA,2026-01-04T12:00:00Z\n")  # 落入 (1.3,1.5]
    rows = estimate_rows(client)
    assert rows[canon("2026-01-02")][0] == logic.EST_ADOPTED       # 前一段不受影响
    seg2 = rows[canon("2026-01-04")]
    assert seg2[0] == logic.EST_REJECTED and seg2[1] == logic.EST_REASON_SWAP


# ---------------------------------------------------------------- 同刻让位

def test_estimate_superseded_by_same_timestamp_actual(client):
    upload(client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,100\n")
    # 同刻估读：直接以 superseded 保存，不报错
    resp = upload(client, "estimates",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,105\n")
    assert "新增 1 条" in resp.get_data(as_text=True)
    rows = estimate_rows(client)
    status, reason, _ = rows[canon("2026-01-01")]
    assert status == logic.EST_REJECTED and reason == logic.EST_REASON_SUPERSEDED


def test_late_actual_supersedes_estimate_even_with_different_value(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n")
    assert estimate_rows(client)[canon()][0] == logic.EST_ADOPTED

    # 补同刻实抄，值与估读不同也正常入库，估读让位；剩余分段重核
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,125\n")
    assert "上传成功" in resp.get_data(as_text=True)
    status, reason, val = estimate_rows(client)[canon()]
    assert status == logic.EST_REJECTED and reason == logic.EST_REASON_SUPERSEDED
    assert val == "120"  # 估读原值保留
    # 实抄口径使用新实抄：100->125->160
    actual = usage(client, "A", "2026-01-01", "2026-01-03")
    assert [d["kwh"] for d in actual["days"]] == ["0", "25", "35"]


def test_actual_conflict_rules_unchanged_with_estimates_present(client):
    # 实抄之间的同刻不同值冲突仍然整批拒绝（估读不改变实抄原规则）
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,151\n")
    assert "已存在不同读数" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"


# ---------------------------------------------------------------- 事务与回滚

def test_estimate_conflict_rolls_back_and_keeps_status(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,140\n")
    before = estimate_rows(client)
    # 同刻不同估读 -> 整批拒绝：新行不入库，旧状态保留
    resp = upload(client, "estimates",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,999\n"
                  "A,2026-01-05T00:00:00Z,170\n")
    assert "已存在不同估读" in resp.get_data(as_text=True) and "整批拒绝" in resp.get_data(as_text=True)
    assert estimate_rows(client) == before
    # 被拒批次留有审计记录
    with client.application.app_context():
        from app.db import get_db
        rejected = get_db().execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE kind='estimates' AND status='rejected'"
        ).fetchone()["n"]
    assert rejected == 1


def test_estimate_idempotent_reupload(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    csv_text = "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
    upload(client, "estimates", csv_text)
    resp = upload(client, "estimates", csv_text)
    assert "重复跳过 1 条" in resp.get_data(as_text=True)
    rows = estimate_rows(client)
    assert len(rows) == 1 and rows[canon()][0] == logic.EST_ADOPTED


def test_actual_backwards_batch_keeps_estimate_statuses(client):
    # 实抄倒退整批回滚时，同事务内的估读重核也一并撤销，状态保持原样
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n")
    before = estimate_rows(client)
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-03T00:00:00Z,90\n")  # 倒退
    assert "读数倒退" in resp.get_data(as_text=True)
    assert estimate_rows(client) == before


# ---------------------------------------------------------------- 多计量点隔离

def test_segments_scoped_per_point(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n"
           "B,2026-01-01T00:00:00Z,50\n"
           "B,2026-01-03T00:00:00Z,80\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,200\n"   # A 段倒退
           "B,2026-01-02T00:00:00Z,65\n")                   # B 段正常
    rows_a = estimate_rows(client, "A")
    rows_b = estimate_rows(client, "B")
    assert list(rows_a.values())[0][0] == logic.EST_REJECTED
    assert list(rows_b.values())[0][0] == logic.EST_ADOPTED


def test_swap_uploaded_before_readings_still_gaps_segment(client):
    # 上传顺序不影响判定：先有换表，后补读数/估读，分段仍按含换表处理
    upload(client, "swaps", "point,ts\nA,2026-01-02T12:00:00Z\n")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,140\n")
    rows = estimate_rows(client)
    assert all(v[0] == logic.EST_REJECTED for v in rows.values())
    assert all(v[1] == logic.EST_REASON_SWAP for v in rows.values())
    prov = usage(client, "A", "2026-01-01", "2026-01-04", basis="provisional")
    assert prov["total_kwh"] == "0" and len(prov["gaps"]) == 1


def test_estimate_only_point_not_served_by_actual_basis(client):
    upload(client, "estimates", "point,ts,value\nC,2026-01-01T00:00:00Z,10\n")
    # 显式范围：实抄/暂估口径都不供数（估读无界未采纳）
    resp = client.get("/api/usage?point=C&start=2026-01-01&end=2026-01-02")
    assert resp.status_code == 200 and resp.get_json()["total_kwh"] == "0"
    prov = usage(client, "C", "2026-01-01", "2026-01-02", basis="provisional")
    assert all(d["estimated"] is False for d in prov["days"])
    # 缺省日期范围：沿用原提示
    page = client.get("/usage?point=C")
    assert "暂无实抄读数" in page.get_data(as_text=True)


def test_same_value_same_timestamp_still_supersedes(client):
    # 同刻实抄即使与估读同值，估读依然让位（采纳只看数据来源，不看值是否相等）
    upload(client, "readings",
           "point,ts,value\nE,2026-01-01T00:00:00Z,100\n"
           "E,2026-01-03T00:00:00Z,160\n")
    upload(client, "estimates", "point,ts,value\nE,2026-01-02T00:00:00Z,130\n")
    upload(client, "readings", "point,ts,value\nE,2026-01-02T00:00:00Z,130\n")
    with client.application.app_context():
        from app.db import get_db
        r = get_db().execute(
            "SELECT status, reason FROM estimates WHERE point='E'"
        ).fetchone()
    assert r["status"] == logic.EST_REJECTED
    assert r["reason"] == logic.EST_REASON_SUPERSEDED


# ---------------------------------------------------------------- 页面与接口

def test_estimates_page_and_markers(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    upload(client, "estimates", "point,ts,value\nA,2026-01-02T00:00:00Z,120\n")
    # 估读状态页
    body = client.get("/estimates?point=A").get_data(as_text=True)
    assert "已采纳" in body and "2026-01-02" in body
    # 首页含估读上传入口与状态表
    home = client.get("/").get_data(as_text=True)
    assert "上传估读" in home and "估读采纳状态" in home
    # 用量页暂估口径带标记文案
    page = client.get(
        "/usage?point=A&start=2026-01-01&end=2026-01-03&basis=provisional"
    ).get_data(as_text=True)
    assert "含以估读为端点的增量" in page and "暂估口径" in page
    # 默认实抄口径页面不含标记
    page_actual = client.get(
        "/usage?point=A&start=2026-01-01&end=2026-01-03"
    ).get_data(as_text=True)
    assert "实抄口径" in page_actual


def test_api_default_basis_is_actual(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    upload(client, "estimates", "point,ts,value\nA,2026-01-02T00:00:00Z,120\n")
    default = client.get(
        "/api/usage?point=A&start=2026-01-01&end=2026-01-03"
    ).get_json()
    assert default["basis"] == "actual"
    assert [d["kwh"] for d in default["days"]] == ["0", "0", "60"]
    # 显式 actual 与默认一致
    explicit = usage(client, "A", "2026-01-01", "2026-01-03", basis="actual")
    assert explicit["days"] == default["days"]
    # 非法口径 400
    resp = client.get(
        "/api/usage?point=A&start=2026-01-01&end=2026-01-03&basis=guess"
    )
    assert resp.status_code == 400


def test_estimated_day_marker_boundaries(client):
    # 标记规则：增量任一端点为估读 -> 后次读数所属日被标记
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-04T00:00:00Z,160\n")
    upload(client, "estimates",
           "point,ts,value\nA,2026-01-02T00:00:00Z,120\n"
           "A,2026-01-03T00:00:00Z,140\n")
    prov = usage(client, "A", "2026-01-01", "2026-01-04", basis="provisional")
    # 1.1 是首条实抄，无增量 -> 不标记；其后三天的增量都含估读端点
    flags = {d["day"]: d["estimated"] for d in prov["days"]}
    assert flags == {
        "2026-01-01": False,
        "2026-01-02": True,   # 实抄->估读
        "2026-01-03": True,   # 估读->估读
        "2026-01-04": True,   # 估读->实抄
    }
