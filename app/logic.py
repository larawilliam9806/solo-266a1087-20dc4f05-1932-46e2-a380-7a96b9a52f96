"""核心业务规则。

计量模型
--------
每个计量点的读数按时刻排序，相邻两条读数构成一个区间 (t1, t2]：

* 若区间 (t1, t2] 内发生过换表（换表时刻 s 满足 t1 < s <= t2，即含后次读数时刻），
  该区间标记为缺口，不推算增量；
* 否则增量 = 后次读数 - 前次读数，必须非负（倒退则整批拒绝），
  增量计入后次读数所属的 UTC 日。

晚到读数插入后自然拆分旧区间；补录换表后原区间变为缺口，已计入的增量自动撤销。
日用量不做冗余物化，查询时在单个读事务内由原始记录现算，
因此“原始记录”和“受影响日结果”永远在同一事务里一起生效。

估读（暂估读数）
----------------
电表隔几天才实抄一次时，结算员可用估读把两次相邻实抄之间的电量拆成逐日增量。
估读与实抄读数同 CSV 格式，单独存放，每条带有采纳状态与未采纳原因：

* **同刻让位**：同一时刻已有（或本次补入）实抄时，该刻估读让位于实抄
  （状态 rejected / superseded），即使两者数值不同也不阻拦实抄入库；
* **分段采纳**：取两条相邻实抄 r_i、r_{i+1} 之间的全部中间估读，仅当
  (1) 该实抄区间按原口径没有换表（不是缺口），且
  (2) 连同两端实抄在内的整条读数序列单调不减（插入全部中间估读后不倒退），
  才把这一段的中间估读**一并采纳**；任一条件不满足，这一段全部不采纳；
* 落在首条实抄之前或末条实抄之后的估读，没有两端实抄可校验，一律不采纳；
* 采纳状态随补传增量重核：补传实抄/估读/换表时，只重核本批影响的实抄分段
  及同刻估读，状态与明细在同一事务内生效，失败回滚保留原数据与原状态。

用量口径
~~~~~~~~
* 实抄口径（默认）：只用实抄读数连区间；
* 暂估口径：实抄读数与“已采纳”估读合并后连区间（同刻不会同时存在二者），
  增量仍计入后次读数所属 UTC 日，并标记哪些日期包含以估读为端点的增量。
"""

import bisect
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# 定宽规范化 UTC 时刻：字典序 == 时间序
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_POINT_LEN = 200
MAX_RANGE_DAYS = 400

# 估读采纳状态
EST_ADOPTED = "adopted"
EST_REJECTED = "rejected"

# 估读未采纳原因（rejected 时）
EST_REASON_SUPERSEDED = "superseded"   # 同刻已有实抄，估读让位
EST_REASON_UNBOUNDED = "unbounded"     # 不在两条相邻实抄之间，无法校验
EST_REASON_SWAP = "swap"               # 所在实抄区间内含换表（缺口区间）
EST_REASON_BACKWARDS = "backwards"     # 插入估读后读数倒退

EST_REASON_TEXT = {
    EST_REASON_SUPERSEDED: "同刻已有实抄，估读让位",
    EST_REASON_UNBOUNDED: "不在两条相邻实抄之间，无两端实抄可校验",
    EST_REASON_SWAP: "所在实抄区间内含换表，按缺口处理，不推算增量",
    EST_REASON_BACKWARDS: "连同两端实抄出现读数倒退，整段不采纳",
}

# 用量查询口径
BASIS_ACTUAL = "actual"      # 仅实抄（默认）
BASIS_PROVISIONAL = "provisional"  # 实抄 + 已采纳估读


class CsvError(Exception):
    """CSV 格式或内容非法。"""


class BatchRejected(Exception):
    """批次与既有数据冲突或导致读数倒退，整批拒绝。"""


# ---------------------------------------------------------------- 基础工具

def canon_ts(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def now_ts():
    return canon_ts(datetime.now(timezone.utc))


def parse_ts(raw, where=""):
    """解析 ISO 8601 UTC 时刻，返回规范化字符串。必须显式带 UTC 时区。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}时刻为空")
    t = s[:-1] + "+00:00" if s[-1] in ("Z", "z") else s
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise CsvError(
            f"{where}无法解析的时刻 {s!r}（需 ISO 8601，如 2026-01-02T03:04:05Z）"
        )
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise CsvError(f"{where}时刻 {s!r} 必须显式使用 UTC（后缀 Z 或 +00:00）")
    return canon_ts(dt)


def parse_value(raw, where=""):
    """解析非负十进制千瓦时。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}读数为空")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CsvError(f"{where}非法的十进制读数 {s!r}")
    if not d.is_finite():
        raise CsvError(f"{where}读数须为有限十进制数：{s!r}")
    if d < 0:
        raise CsvError(f"{where}读数不能为负：{s!r}")
    return d


def value_str(d):
    """Decimal 的规范文本形式（去尾零、不用科学计数法）。"""
    return format(d.normalize(), "f")


def parse_day(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"非法日期 {s!r}（需 YYYY-MM-DD）")


def fmt_num(d):
    return value_str(d if isinstance(d, Decimal) else Decimal(d))


def fmt_ts(ts):
    """把规范化时刻显示为 '2026-01-02 03:04:05 UTC'（按需保留小数秒）。"""
    body = ts[:-1].replace("T", " ")
    if "." in body:
        head, frac = body.split(".")
        frac = frac.rstrip("0")
        body = head if not frac else f"{head}.{frac}"
    return body + " UTC"


# ---------------------------------------------------------------- CSV 解析

def _iter_rows(text, required):
    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as e:
        raise CsvError(f"CSV 解析失败：{e}")
    if reader.fieldnames is None:
        raise CsvError("CSV 为空或缺少表头")
    keymap = {}
    for name in reader.fieldnames:
        keymap.setdefault((name or "").strip().lower(), name)
    missing = [c for c in required if c not in keymap]
    if missing:
        raise CsvError(
            f"CSV 缺少必需列：{', '.join(missing)}（需要表头：{', '.join(required)}）"
        )
    for lineno, row in enumerate(reader, start=2):
        if row is None:
            continue
        cells = {k: (row.get(keymap[k]) or "") for k in required}
        if all(not c.strip() for c in cells.values()):
            continue  # 跳过空行
        yield lineno, cells


def parse_readings_csv(text):
    """返回 ({(point, ts): Decimal}, 原始数据行数)。同点同刻不同读数 -> CsvError。"""
    out = {}
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts", "value"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        val = parse_value(cells["value"], where)
        key = (point, ts)
        if key in out:
            if out[key] != val:
                raise CsvError(
                    f"{where}同一计量点同一时刻出现不同读数"
                    f"（{value_str(out[key])} 与 {value_str(val)}），整批拒绝"
                )
            continue  # 同值重复行：幂等去重
        out[key] = val
    return out, raw_count


def parse_swaps_csv(text):
    """返回 ({(point, ts)}, 原始数据行数）。"""
    out = set()
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        key = (point, ts)
        if key in out:
            continue  # 重复行：幂等去重
        out.add(key)
    return out, raw_count


# ---------------------------------------------------------------- 区间与用量

def point_state(conn, point, basis=BASIS_ACTUAL):
    """读取一个计量点的读数/换表，计算相邻读数区间。

    basis:
      BASIS_ACTUAL      —— 只用实抄读数（原口径）；
      BASIS_PROVISIONAL —— 实抄读数并入已采纳估读，按时刻合并排序。

    返回 (readings, swaps, intervals)；readings 行为 {'ts','value','kind'}，
    kind 取值 actual / estimate。interval 形如：
    {'start', 'end', 'v1', 'v2', 'gap', 'delta', 'start_kind', 'end_kind'}，
    gap 区间 delta 为 None。
    """
    if basis == BASIS_PROVISIONAL:
        rows = conn.execute(
            "SELECT ts, value, 'actual' AS kind FROM readings WHERE point = ?"
            " UNION ALL"
            " SELECT ts, value, 'estimate' AS kind FROM estimates"
            " WHERE point = ? AND status = 'adopted'"
            " ORDER BY ts",
            (point, point),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, value, 'actual' AS kind FROM readings"
            " WHERE point = ? ORDER BY ts",
            (point,),
        ).fetchall()
    swaps = [
        r["ts"]
        for r in conn.execute(
            "SELECT ts FROM swaps WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]
    intervals = []
    prev = None
    for row in rows:
        ts, val, kind = row["ts"], Decimal(row["value"]), row["kind"]
        if prev is not None:
            t1, v1, k1 = prev
            i = bisect.bisect_right(swaps, t1)
            gap = i < len(swaps) and swaps[i] <= ts  # 换表落在 (t1, t2]，含后次时刻
            intervals.append(
                {
                    "start": t1,
                    "end": ts,
                    "v1": v1,
                    "v2": val,
                    "gap": gap,
                    "delta": None if gap else val - v1,
                    "start_kind": k1,
                    "end_kind": kind,
                }
            )
        prev = (ts, val, kind)
    return rows, swaps, intervals


def find_negative_deltas(conn, point):
    """该计量点所有非缺口区间中读数倒退的区间描述（空列表表示正常）。"""
    _, _, intervals = point_state(conn, point)
    errors = []
    for it in intervals:
        if not it["gap"] and it["delta"] < 0:
            errors.append(
                f"计量点 {point}：{fmt_ts(it['start'])} 读数 {value_str(it['v1'])} "
                f"大于 {fmt_ts(it['end'])} 读数 {value_str(it['v2'])}"
                f"（倒退 {value_str(-it['delta'])} kWh）"
            )
    return errors


def usage_range(conn, point, start_day, end_day, basis=BASIS_ACTUAL):
    """计算 [start_day, end_day]（YYYY-MM-DD，含两端）逐日用量与缺口区间。

    basis=BASIS_ACTUAL（默认）仅按实抄供数；
    basis=BASIS_PROVISIONAL 并入已采纳估读（暂估日用量）。

    返回 (days, gaps, total)：
      days  — [{'day', 'kwh', 'gap', 'estimated'}]，范围内每一天一行，
              gap 表示当日被缺口覆盖；estimated 表示当日至少含一条
              以估读为端点（前次或后次为估读）的增量；
      gaps  — 与范围相交的缺口区间 [{'start', 'end'}]；
      total — 范围内用量合计（Decimal）。
    """
    _, _, intervals = point_state(conn, point, basis=basis)
    lo = start_day + "T00:00:00.000000Z"
    hi = (parse_day(end_day) + timedelta(days=1)).strftime(
        "%Y-%m-%dT00:00:00.000000Z"
    )
    daily = defaultdict(Decimal)
    estimated_days = set()
    gaps = []
    for it in intervals:
        if it["gap"]:
            if it["end"] >= lo and it["start"] < hi:  # 与所选范围相交
                gaps.append({"start": it["start"], "end": it["end"]})
        else:
            day = it["end"][:10]  # 增量计入后次读数所属 UTC 日
            if start_day <= day <= end_day:
                daily[day] += it["delta"]
                if (
                    it["end_kind"] == "estimate"
                    or it["start_kind"] == "estimate"
                ):
                    estimated_days.add(day)

    def gap_on(day):
        d_lo = day + "T00:00:00.000000Z"
        d_hi = (parse_day(day) + timedelta(days=1)).strftime(
            "%Y-%m-%dT00:00:00.000000Z"
        )
        return any(g["end"] >= d_lo and g["start"] < d_hi for g in gaps)

    days = []
    d = parse_day(start_day)
    last = parse_day(end_day)
    while d <= last:
        key = d.strftime("%Y-%m-%d")
        days.append(
            {
                "day": key,
                "kwh": daily.get(key, Decimal(0)),
                "gap": gap_on(key),
                "estimated": key in estimated_days,
            }
        )
        d += timedelta(days=1)
    total = sum((x["kwh"] for x in days), Decimal(0))
    return days, gaps, total


# ---------------------------------------------------------------- 批量导入
# 以下函数在调用方开启的事务内执行；抛出 BatchRejected 时由调用方回滚。
# 估读采纳状态随每次导入增量重核：只重核本批影响的实抄分段及同刻估读，
# 状态与明细在同一事务内生效；抛错回滚即保留原数据与原状态。

def _neighbor_actuals(conn, point, ts):
    """返回该时刻两侧最近的实抄 (row_prev, row_next)，可为 None。

    用严格不等号：调用时点可能本身已是实抄（补传实抄的重核），
    不能把新插入的自己当作相邻点。
    """
    prev_row = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? AND ts < ?"
        " ORDER BY ts DESC LIMIT 1",
        (point, ts),
    ).fetchone()
    next_row = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? AND ts > ?"
        " ORDER BY ts ASC LIMIT 1",
        (point, ts),
    ).fetchone()
    return prev_row, next_row


def _segment_actuals(conn, point, lo_ts, hi_ts):
    """取闭区间 [lo_ts, hi_ts] 内该点的全部实抄（按时刻升序）。"""
    return conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? AND ts >= ? AND ts <= ?"
        " ORDER BY ts",
        (point, lo_ts, hi_ts),
    ).fetchall()


def _segment_has_swap(conn, point, lo_ts, hi_ts):
    """换表是否落在 (lo_ts, hi_ts]（与区间缺口口径一致，含后次时刻）。"""
    return conn.execute(
        "SELECT 1 FROM swaps WHERE point = ? AND ts > ? AND ts <= ? LIMIT 1",
        (point, lo_ts, hi_ts),
    ).fetchone() is not None


def _set_estimate_status(conn, point, lo_ts, hi_ts, status, reason):
    """把该点 (lo_ts, hi_ts) 开区间内的估读统一置为指定状态/原因。"""
    conn.execute(
        "UPDATE estimates SET status = ?, reason = ?"
        " WHERE point = ? AND ts > ? AND ts < ?",
        (status, reason, point, lo_ts, hi_ts),
    )


def reevaluate_segments(conn, point, segments):
    """对若干实抄分段 (lo_ts, hi_ts) 重核其中间估读的采纳状态。

    分段采纳规则（整段同进同退）：
      1. 分段两端必须都是实抄，且区间内按原口径无换表；
      2. 两端实抄 + 全部分段内估读按时刻排列后单调不减（不倒退）。
    任一不满足：该分段内全部估读 rejected 并记录对应原因。
    """
    for lo_ts, hi_ts in sorted(set(segments)):
        rows = _segment_actuals(conn, point, lo_ts, hi_ts)
        if len(rows) < 2:  # 两端实抄缺一：分段不成立，估读留待后续补传单列处理
            continue
        est_rows = conn.execute(
            "SELECT ts, value FROM estimates"
            " WHERE point = ? AND ts > ? AND ts < ? ORDER BY ts",
            (point, lo_ts, hi_ts),
        ).fetchall()
        if not est_rows:
            continue  # 分段内没有估读，无需更新状态
        # 规则 1：换表缺口分段，整段不采纳
        if _segment_has_swap(conn, point, lo_ts, hi_ts):
            _set_estimate_status(
                conn, point, lo_ts, hi_ts, EST_REJECTED, EST_REASON_SWAP
            )
            continue
        # 规则 2：实抄 + 全部中间估读合并后须单调不减
        chain = [(r["ts"], Decimal(r["value"])) for r in rows]
        chain.extend((r["ts"], Decimal(r["value"])) for r in est_rows)
        chain.sort(key=lambda x: x[0])
        backwards = any(
            chain[i][1] < chain[i - 1][1] for i in range(1, len(chain))
        )
        if backwards:
            _set_estimate_status(
                conn, point, lo_ts, hi_ts, EST_REJECTED, EST_REASON_BACKWARDS
            )
        else:
            _set_estimate_status(
                conn, point, lo_ts, hi_ts, EST_ADOPTED, None
            )


def _mark_superseded(conn, point, ts):
    """同刻估读让位于实抄：置 rejected/superseded（无论两者数值是否相同）。"""
    conn.execute(
        "UPDATE estimates SET status = ?, reason = ?"
        " WHERE point = ? AND ts = ? AND status <> ?",
        (EST_REJECTED, EST_REASON_SUPERSEDED, point, ts, EST_REJECTED),
    )


def apply_readings(conn, upload_id, mapping):
    """写入实抄读数。返回 (新增条数, 幂等跳过条数)。

    - 同点同刻同值：幂等跳过，不重复计数；
    - 同点同刻不同值：BatchRejected，整批回滚；
    - 写入后任一受影响计量点出现读数倒退：BatchRejected，整批回滚；
    - 同刻若有估读：估读让位于实抄（不同值也不阻拦实抄）；
    - 新实抄拆分旧分段：仅重核受影响分段内估读的采纳状态。
    """
    applied = skipped = 0
    affected = set()
    new_ts_by_point = defaultdict(list)
    for (point, ts), val in mapping.items():
        row = conn.execute(
            "SELECT value FROM readings WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            if Decimal(row["value"]) == val:
                skipped += 1
                continue
            raise BatchRejected(
                f"计量点 {point} 在 {fmt_ts(ts)} 已存在不同读数"
                f"（现有 {row['value']}，本次 {value_str(val)}），整批拒绝"
            )
        conn.execute(
            "INSERT INTO readings(point, ts, value, upload_id) VALUES (?,?,?,?)",
            (point, ts, value_str(val), upload_id),
        )
        applied += 1
        affected.add(point)
        new_ts_by_point[point].append(ts)
        # 同刻估读让位实抄（即使值不同也不阻拦）
        _mark_superseded(conn, point, ts)

    errors = []
    for p in sorted(affected):
        errors.extend(find_negative_deltas(conn, p))
    if errors:
        raise BatchRejected("读数倒退，整批拒绝：" + "；".join(errors[:5]))

    # 增量重核：每条新实抄所在（拆分后）的相邻实抄分段
    for point, ts_list in new_ts_by_point.items():
        segments = set()
        for ts in ts_list:
            prev_row, next_row = _neighbor_actuals(conn, point, ts)
            if prev_row is not None:
                segments.add((prev_row["ts"], ts))
            if next_row is not None and next_row["ts"] != ts:
                segments.add((ts, next_row["ts"]))
        reevaluate_segments(conn, point, segments)
    return applied, skipped


def apply_estimates(conn, upload_id, mapping):
    """写入估读。返回 (新增条数, 幂等跳过条数, 已采纳条数, 未采纳条数)。

    - 同点同刻同值：幂等跳过（保留既有采纳状态，不重复计数）；
    - 同点同刻不同值：BatchRejected，整批回滚（明细与状态都不变）；
    - 同刻已有实抄：估读直接以 rejected/superseded 保存，不阻拦任何数据；
    - 其余估读先落库为 rejected/unbounded，再仅重核受影响实抄分段。
    """
    applied = skipped = 0
    new_ts_by_point = defaultdict(list)
    for (point, ts), val in mapping.items():
        row = conn.execute(
            "SELECT value FROM estimates WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            if Decimal(row["value"]) == val:
                skipped += 1
                continue
            raise BatchRejected(
                f"计量点 {point} 在 {fmt_ts(ts)} 已存在不同估读"
                f"（现有 {row['value']}，本次 {value_str(val)}），整批拒绝"
            )
        same_actual = conn.execute(
            "SELECT 1 FROM readings WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if same_actual is not None:
            status, reason = EST_REJECTED, EST_REASON_SUPERSEDED
        else:
            status, reason = EST_REJECTED, EST_REASON_UNBOUNDED
        conn.execute(
            "INSERT INTO estimates(point, ts, value, status, reason, upload_id)"
            " VALUES (?,?,?,?,?,?)",
            (point, ts, value_str(val), status, reason, upload_id),
        )
        applied += 1
        new_ts_by_point[point].append(ts)

    # 增量重核：新估读所在的相邻实抄分段；无两端实抄则保持 unbounded
    for point, ts_list in new_ts_by_point.items():
        segments = set()
        for ts in ts_list:
            prev_row, next_row = _neighbor_actuals(conn, point, ts)
            if prev_row is not None and next_row is not None:
                segments.add((prev_row["ts"], next_row["ts"]))
        reevaluate_segments(conn, point, segments)

    stats = conn.execute(
        "SELECT status, COUNT(*) AS n FROM estimates WHERE upload_id = ?"
        " GROUP BY status",
        (upload_id,),
    ).fetchall()
    adopted = sum(r["n"] for r in stats if r["status"] == EST_ADOPTED)
    rejected = sum(r["n"] for r in stats if r["status"] == EST_REJECTED)
    return applied, skipped, adopted, rejected


def apply_swaps(conn, upload_id, keys):
    """写入换表记录。返回 (新增条数, 幂等跳过条数)。

    换表只会把区间标记为缺口（撤销已计入增量），不会产生倒退，无需额外校验。
    补录换表后仅重核其落入的实抄分段：分段内估读按缺口规则整段不采纳。
    """
    applied = skipped = 0
    new_swaps = defaultdict(list)
    for point, ts in sorted(keys):
        row = conn.execute(
            "SELECT 1 FROM swaps WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO swaps(point, ts, upload_id) VALUES (?,?,?)",
            (point, ts, upload_id),
        )
        applied += 1
        new_swaps[point].append(ts)

    # 增量重核：换表时刻 s 落在 (t1, t2] 的实抄分段（t1 < s <= t2）
    for point, ts_list in new_swaps.items():
        segments = set()
        for s in ts_list:
            lo_row = conn.execute(
                "SELECT ts FROM readings WHERE point = ? AND ts < ?"
                " ORDER BY ts DESC LIMIT 1",
                (point, s),
            ).fetchone()
            hi_row = conn.execute(
                "SELECT ts FROM readings WHERE point = ? AND ts >= ?"
                " ORDER BY ts ASC LIMIT 1",
                (point, s),
            ).fetchone()
            if lo_row is not None and hi_row is not None:
                segments.add((lo_row["ts"], hi_row["ts"]))
        reevaluate_segments(conn, point, segments)
    return applied, skipped


# ---------------------------------------------------------------- 估读查询

def normalize_basis(raw):
    """校验用量口径参数，非法值抛 ValueError。"""
    s = (raw or "").strip() or BASIS_ACTUAL
    if s not in (BASIS_ACTUAL, BASIS_PROVISIONAL):
        raise ValueError(
            f"非法口径 {s!r}（可选 {BASIS_ACTUAL} 或 {BASIS_PROVISIONAL}）"
        )
    return s


def reason_text(reason):
    """把估读未采纳原因代码转为中文说明。"""
    if not reason:
        return ""
    return EST_REASON_TEXT.get(reason, reason)


def list_estimates(conn, point=None, limit=500):
    """查看已保存估读的采纳状态与未采纳原因（默认最近若干条，按批次/时刻倒序）。"""
    if point:
        return conn.execute(
            "SELECT e.point, e.ts, e.value, e.status, e.reason, e.upload_id,"
            "       u.created_at AS uploaded_at"
            " FROM estimates e JOIN uploads u ON u.id = e.upload_id"
            " WHERE e.point = ? ORDER BY e.ts DESC, e.point LIMIT ?",
            (point, limit),
        ).fetchall()
    return conn.execute(
        "SELECT e.point, e.ts, e.value, e.status, e.reason, e.upload_id,"
        "       u.created_at AS uploaded_at"
        " FROM estimates e JOIN uploads u ON u.id = e.upload_id"
        " ORDER BY e.upload_id DESC, e.point, e.ts LIMIT ?",
        (limit,),
    ).fetchall()
