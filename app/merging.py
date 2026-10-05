"""两份已保存观测版本的合并：预检分析 + 严格提交。

合并规则（见 README「版本合并」）：

1. 两版的观测点、UTC 日期范围、夜间（暮光）模式、最低高度、最大空气质量
   必须完全一致，任一不一致即拒绝合并（硬性冲突，无法在页面上选择消解）。
2. 目标按 ID 去重：仅出现在一版的目标直接并入；同 ID 目标若整组定义
   （参数：名称/赤经/赤纬/曝光/优先级，或手动固定时段）不同，构成冲突，
   必须由调用方逐个指定保留「基准版 base」还是「补充版 supplement」的
   **整组定义**（含该版的手动固定时段），未指定则拒绝合并。
3. 校准按 ID 去重：同 ID 校准定义不同直接拒绝；合并后的校准时段彼此
   重叠也直接拒绝。
4. 合并后用同一个 build_plan 重算全部可行窗口与排程；选中定义中的手动
   固定时段必须全部保留：若因目标变更（离开可行窗口）、与其他固定时段
   相撞、或占用校准而无法保留，逐个指出目标与时段，整次合并失败
   （绝不降级为 warning 后照常保存）。
"""

from __future__ import annotations

from datetime import timedelta

from .astronomy import PlanError
from .planner import build_plan
from .scheduling_utils import parse_coord, parse_dec, parse_ra, parse_utc

BASE = "base"
SUPPLEMENT = "supplement"
SIDES = (BASE, SUPPLEMENT)


class MergeError(PlanError):
    """合并被拒绝；details 携带结构化冲突信息供前端展示。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


# ------------------------------------------------------------------ 工具函数
def _iso(grid_start, minute: int) -> str:
    return (grid_start + timedelta(minutes=int(minute))).strftime("%Y-%m-%dT%H:%MZ")


def _grid(req: dict):
    """返回 (grid_start, n_minutes)，与 planner.build_plan 的网格定义一致。"""
    start_day = parse_utc(req.get("start_date"))
    end_day = parse_utc(req.get("end_date"), end_of_day=True)
    grid_start = start_day.replace(hour=0, minute=0, second=0)
    grid_end = end_day.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    n = int((grid_end - grid_start).total_seconds() // 60)
    if n <= 0:
        raise PlanError("版本的日期范围无效，无法合并")
    return grid_start, n


def _fclose(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _pins_map(req: dict) -> dict[str, int]:
    """request.pins: target_id -> 起始分钟（规范化为 int；无法解析则忽略）。"""
    out: dict[str, int] = {}
    pins = req.get("pins")
    if isinstance(pins, dict):
        for tid, p in pins.items():
            if isinstance(p, dict):
                try:
                    out[str(tid)] = int(round(float(p.get("start_min"))))
                except (TypeError, ValueError):
                    continue
    return out


def _target_canon(raw: dict, index: int) -> dict:
    """单个目标原始定义 -> 规范化整组定义（解析坐标/数值，补默认值）。"""
    tid = str(raw.get("id") or f"T{index + 1}").strip()
    name = str(raw.get("name") or tid).strip() or tid
    ra = parse_ra(raw.get("ra"))
    dec = parse_dec(raw.get("dec"))
    try:
        exposure = int(float(raw.get("exposure_minutes")))
    except (TypeError, ValueError):
        raise PlanError(f"目标「{name}」的曝光时长不是数字")
    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError):
        raise PlanError(f"目标「{name}」的优先级不是整数")
    return {
        "id": tid,
        "name": name,
        "ra": ra,
        "dec": dec,
        "exposure": exposure,
        "priority": priority,
    }


def _pin_interval(c: dict, grid_start, n: int):
    """校准原始定义 -> 有效（裁剪到网格后的）半开区间 [m0,m1)；范围外返回 None。"""
    t0 = parse_utc(c.get("start"))
    t1 = parse_utc(c.get("end"))
    m0 = max(0, int((t0 - grid_start).total_seconds() // 60))
    m1 = min(n, int((t1 - grid_start).total_seconds() // 60))
    if m1 <= m0:
        return None
    return m0, m1


def _cal_canon(raw: dict, index: int, grid_start, n: int) -> dict:
    cid = str(raw.get("id") or f"C{index + 1}").strip()
    label = str(raw.get("label") or f"校准 {index + 1}").strip() or f"校准 {index + 1}"
    iv = _pin_interval(raw, grid_start, n)
    return {"id": cid, "label": label, "iv": iv}


def _target_view(raw: dict, pin: int | None, grid_start) -> dict:
    """预检响应里单侧目标摘要（保留原始输入字符串，便于页面对照）。"""
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or raw.get("id") or ""),
        "ra": raw.get("ra"),
        "dec": raw.get("dec"),
        "exposure_minutes": raw.get("exposure_minutes"),
        "priority": raw.get("priority"),
        "pin": (
            None
            if pin is None
            else {
                "start_min": pin,
                "end_min": pin + int(float(raw.get("exposure_minutes"))),
                "start": _iso(grid_start, pin),
                "end": _iso(grid_start, pin + int(float(raw.get("exposure_minutes")))),
            }
        ),
    }


def _cal_view(canon: dict, grid_start) -> dict:
    iv = canon["iv"]
    return {
        "id": canon["id"],
        "label": canon["label"],
        "start_min": None if iv is None else iv[0],
        "end_min": None if iv is None else iv[1],
        "start": None if iv is None else _iso(grid_start, iv[0]),
        "end": None if iv is None else _iso(grid_start, iv[1]),
    }


# ============================================================ 1) 硬性环境比对
def _check_context(base: dict, supp: dict) -> tuple[dict, list[dict]]:
    """比对观测点/日期/约束；返回公共环境字典与硬冲突列表。"""
    hard: list[dict] = []

    try:
        b_lat = parse_coord((base.get("site") or {}).get("lat"), "lat")
        b_lon = parse_coord((base.get("site") or {}).get("lon"), "lon")
        s_lat = parse_coord((supp.get("site") or {}).get("lat"), "lat")
        s_lon = parse_coord((supp.get("site") or {}).get("lon"), "lon")
    except PlanError as exc:
        raise MergeError(f"版本观测点数据无法解析：{exc}")
    if not _fclose(b_lat, s_lat) or not _fclose(b_lon, s_lon):
        hard.append({
            "kind": "site",
            "message": f"观测点不一致：基准版（{b_lat:g}°, {b_lon:g}°）"
                       f"≠ 补充版（{s_lat:g}°, {s_lon:g}°），拒绝合并",
        })

    try:
        b_start = parse_utc(base.get("start_date"))
        b_end = parse_utc(base.get("end_date"))
        s_start = parse_utc(supp.get("start_date"))
        s_end = parse_utc(supp.get("end_date"))
    except PlanError as exc:
        raise MergeError(f"版本日期数据无法解析：{exc}")
    if b_start.date() != s_start.date() or b_end.date() != s_end.date():
        hard.append({
            "kind": "date_range",
            "message": f"UTC 日期范围不一致：基准版 {b_start.date():%Y-%m-%d} ~ "
                       f"{b_end.date():%Y-%m-%d}，补充版 {s_start.date():%Y-%m-%d} ~ "
                       f"{s_end.date():%Y-%m-%d}，拒绝合并",
        })

    b_mode = str(base.get("twilight_mode", "night"))
    s_mode = str(supp.get("twilight_mode", "night"))
    if b_mode != s_mode:
        hard.append({
            "kind": "twilight_mode",
            "message": f"夜间/暮光约束不一致：基准版 {b_mode!r} ≠ 补充版 {s_mode!r}，拒绝合并",
        })

    try:
        b_alt = float(base.get("min_altitude", 20.0))
        s_alt = float(supp.get("min_altitude", 20.0))
    except (TypeError, ValueError):
        raise MergeError("版本最低高度数据无法解析")
    if not _fclose(b_alt, s_alt):
        hard.append({
            "kind": "min_altitude",
            "message": f"最低高度约束不一致：基准版 {b_alt:g}° ≠ 补充版 {s_alt:g}°，拒绝合并",
        })

    try:
        b_x = float(base.get("max_airmass", 2.5))
        s_x = float(supp.get("max_airmass", 2.5))
    except (TypeError, ValueError):
        raise MergeError("版本最大空气质量数据无法解析")
    if not _fclose(b_x, s_x):
        hard.append({
            "kind": "max_airmass",
            "message": f"最大空气质量约束不一致：基准版 {b_x:g} ≠ 补充版 {s_x:g}，拒绝合并",
        })

    grid_start, n = _grid(base)
    context = {
        "site": {"lat": b_lat, "lon": b_lon},
        "start_date": base.get("start_date"),
        "end_date": base.get("end_date"),
        "grid_start": _iso(grid_start, 0),
        "minutes": n,
        "twilight_mode": b_mode,
        "min_altitude": b_alt,
        "max_airmass": b_x,
    }
    return context, hard


# ============================================================ 2) 目标比对
_PARAM_FIELDS = [
    ("name", "名称"),
    ("ra", "赤经"),
    ("dec", "赤纬"),
    ("exposure", "曝光时长"),
    ("priority", "优先级"),
]


def _index_targets(req: dict):
    """返回 (raw_by_id, canon_by_id, pins)，保持版本内顺序。"""
    raws, canon = {}, {}
    for i, t in enumerate(req.get("targets") or []):
        c = _target_canon(t, i)
        raws[c["id"]] = t
        canon[c["id"]] = c
    return raws, canon, _pins_map(req)


def prepare_merge(base_req: dict, supp_req: dict) -> dict:
    """预检：返回冲突分析（不执行合并，即使存在冲突也正常返回 200）。"""
    context, hard = _check_context(base_req, supp_req)
    grid_start, n = _grid(base_req)

    b_raw, b_can, b_pins = _index_targets(base_req)
    s_raw, s_can, s_pins = _index_targets(supp_req)
    all_ids = list(dict.fromkeys(list(b_can) + list(s_can)))

    targets_out = []
    conflict_ids = []
    for tid in all_ids:
        cb, cs = b_can.get(tid), s_can.get(tid)
        pin_b = b_pins.get(tid)
        pin_s = s_pins.get(tid)
        if cb and not cs:
            status = "base_only"
            differ, pin_diff = [], False
        elif cs and not cb:
            status = "supplement_only"
            differ, pin_diff = [], False
        else:
            differ = [
                key for key, _label in _PARAM_FIELDS if cb[key] != cs[key]
            ]
            pin_diff = pin_b != pin_s
            status = "identical" if not differ and not pin_diff else "conflict"
            if status == "conflict":
                conflict_ids.append(tid)
        targets_out.append({
            "id": tid,
            "name": (cb or cs)["name"],
            "status": status,
            "differ_fields": differ,
            "pin_diff": pin_diff,
            "base": None if cb is None else _target_view(b_raw[tid], pin_b, grid_start),
            "supplement": None if cs is None else _target_view(s_raw[tid], pin_s, grid_start),
        })

    # ------------------------------------------------------------ 校准比对
    b_cals = [_cal_canon(c, i, grid_start, n)
              for i, c in enumerate(base_req.get("calibrations") or [])]
    s_cals = [_cal_canon(c, i, grid_start, n)
              for i, c in enumerate(supp_req.get("calibrations") or [])]
    b_cal_map = {c["id"]: c for c in b_cals}
    s_cal_map = {c["id"]: c for c in s_cals}

    cals_out, cal_conflicts, merged_cals = [], [], []
    seen_cal: set[str] = set()
    for c in b_cals:  # 先基准版顺序，再补仅补充版有的
        other = s_cal_map.get(c["id"])
        if other is None:
            cals_out.append({"id": c["id"], "label": c["label"], "status": "base_only",
                             "base": _cal_view(c, grid_start), "supplement": None})
        elif c["label"] == other["label"] and c["iv"] == other["iv"]:
            cals_out.append({"id": c["id"], "label": c["label"], "status": "identical",
                             "base": _cal_view(c, grid_start),
                             "supplement": _cal_view(other, grid_start)})
        else:
            cal_conflicts.append({"id": c["id"], "base": _cal_view(c, grid_start),
                                  "supplement": _cal_view(other, grid_start)})
            cals_out.append({"id": c["id"], "label": c["label"], "status": "conflict",
                             "base": _cal_view(c, grid_start),
                             "supplement": _cal_view(other, grid_start)})
        merged_cals.append(c)
        seen_cal.add(c["id"])
    for c in s_cals:
        if c["id"] in seen_cal:
            continue
        cals_out.append({"id": c["id"], "label": c["label"], "status": "supplement_only",
                         "base": None, "supplement": _cal_view(c, grid_start)})
        merged_cals.append(c)
        seen_cal.add(c["id"])

    # 合并后校准时段相撞（同 ID 定义不同的已单独报冲突，这里只比有效区间）
    cal_overlaps = []
    active = [c for c in merged_cals if c["iv"] is not None
              and c["id"] not in {x["id"] for x in cal_conflicts}]
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            a0, a1 = a["iv"]
            b0, b1 = b["iv"]
            if a0 < b1 and b0 < a1:
                cal_overlaps.append({
                    "a": {"id": a["id"], "label": a["label"], **_iv_view(a["iv"], grid_start)},
                    "b": {"id": b["id"], "label": b["label"], **_iv_view(b["iv"], grid_start)},
                })

    return {
        "context": context,
        "compatible": not (hard or cal_conflicts or cal_overlaps),
        "hard_mismatches": hard,
        "targets": targets_out,
        "target_conflict_ids": conflict_ids,
        "calibrations": cals_out,
        "cal_conflicts": cal_conflicts,
        "cal_overlaps": cal_overlaps,
        "counts": {
            "targets_base": len(b_can),
            "targets_supplement": len(s_can),
            "targets_merged": len(all_ids),
            "target_conflicts": len(conflict_ids),
            "calibrations_merged": len(merged_cals),
        },
    }


def _iv_view(iv, grid_start) -> dict:
    return {
        "start_min": iv[0],
        "end_min": iv[1],
        "start": _iso(grid_start, iv[0]),
        "end": _iso(grid_start, iv[1]),
        "minutes": iv[1] - iv[0],
    }


# ============================================================ 3) 组装合并请求
def _assemble(base_req: dict, supp_req: dict, resolutions: dict[str, str],
              conflict_ids: set[str]) -> dict:
    """按 resolutions 选取整组定义，组装新的 plan 输入。"""
    b_raw, b_can, b_pins = _index_targets(base_req)
    s_raw, s_can, s_pins = _index_targets(supp_req)
    all_ids = list(dict.fromkeys(list(b_can) + list(s_can)))

    chosen_targets = []
    chosen_side: dict[str, str] = {}
    chosen_pins: dict[str, dict] = {}
    for tid in all_ids:
        in_b, in_s = tid in b_can, tid in s_can
        if in_b and in_s:
            if tid in conflict_ids:
                # 仅在两版确有差异时才需要显式消解
                side = resolutions.get(tid)
                if side not in SIDES:
                    raise MergeError(
                        f"同 ID 目标「{tid}」的定义冲突未指定保留版本",
                        {"unresolved_targets": [tid]},
                    )
            else:
                side = BASE  # 两版定义一致（含手动固定），取基准版
        else:
            side = BASE if in_b else SUPPLEMENT
        raw = b_raw[tid] if side == BASE else s_raw[tid]
        chosen_targets.append(dict(raw))
        chosen_side[tid] = side
        pin = (b_pins if side == BASE else s_pins).get(tid)
        if pin is not None:
            chosen_pins[tid] = {"start_min": pin}

    # 校准：基准版顺序 + 仅补充版有的；同 ID 去重（定义相同取基准版）
    merged_cals = []
    seen: set[str] = set()
    for raw in base_req.get("calibrations") or []:
        merged_cals.append(dict(raw))
        seen.add(str(raw.get("id")))
    for raw in supp_req.get("calibrations") or []:
        if str(raw.get("id")) not in seen:
            merged_cals.append(dict(raw))
            seen.add(str(raw.get("id")))

    site = base_req.get("site") or {}
    return {
        "site": {"lat": site.get("lat"), "lon": site.get("lon")},
        "start_date": base_req.get("start_date"),
        "end_date": base_req.get("end_date"),
        "twilight_mode": str(base_req.get("twilight_mode", "night")),
        "min_altitude": base_req.get("min_altitude", 20.0),
        "max_airmass": base_req.get("max_airmass", 2.5),
        "targets": chosen_targets,
        "calibrations": merged_cals,
        "pins": chosen_pins,
    }, chosen_side


# ============================================================ 4) 固定时段强校验
def _validate_pins_strict(merged_req: dict, result: dict) -> list[dict]:
    """逐个核对手动固定时段是否在合并结果中保留；返回错误列表（空=全部保留）。"""
    grid_start, n = _grid(merged_req)
    targets = {str(t["id"]): t for t in (result.get("targets") or [])}
    errors: list[dict] = []

    pin_items = []
    for tid, p in (merged_req.get("pins") or {}).items():
        t = targets.get(str(tid))
        if t is None:
            errors.append({
                "target_id": tid, "name": tid,
                "start_min": None, "end_min": None,
                "start": None, "end": None,
                "reason": "手动固定时段无法保留：合并后的目标列表中不存在该目标",
            })
            continue
        s = int(round(float((p or {}).get("start_min"))))
        e = s + int(t["exposure_minutes"])
        pin_items.append((tid, t, s, e))

    cal_ivs = [
        (c, int(c["start_min"]), int(c["end_min"]))
        for c in result.get("calibrations") or []
    ]

    def _err(tid, t, s, e, reason):
        errors.append({
            "target_id": tid,
            "name": t.get("name") or tid,
            "start_min": s, "end_min": e,
            "start": _iso(grid_start, s) if 0 <= s < n else None,
            "end": _iso(grid_start, e) if 0 < e <= n else None,
            "reason": reason,
        })

    # 4.1 越界 / 离开合并后目标的可行窗口 / 占用校准
    valid_pins: dict[str, tuple[int, int]] = {}
    for tid, t, s, e in pin_items:
        name = t.get("name") or tid
        span = f"{_iso(grid_start, s)}–{_iso(grid_start, e)} UTC"
        if s < 0 or e > n or e <= s:
            _err(tid, t, s, e,
                 f"目标「{name}」的手动固定时段 {span} 超出合并后的 UTC 日期范围，无法保留")
            continue
        if not any(int(w["start_min"]) <= s and e <= int(w["end_min"])
                   for w in t.get("windows") or []):
            _err(tid, t, s, e,
                 f"目标「{name}」的手动固定时段 {span} 因目标变更（坐标/曝光或合并后约束组合），"
                 f"已不在该目标的可行观测窗口内，无法保留")
            continue
        hit = next((c for c, a, b in cal_ivs if s < b and a < e), None)
        if hit is not None:
            _err(tid, t, s, e,
                 f"目标「{name}」的手动固定时段 {span} 与固定校准时段"
                 f"「{hit.get('label')}」{hit.get('start')}–{hit.get('end')} UTC 相撞，无法保留")
            continue
        valid_pins[tid] = (s, e)

    # 4.2 固定时段彼此相撞（双方都要指出）
    items = list(valid_pins.items())
    clash_partners: dict[str, set[str]] = {}
    for i in range(len(items)):
        (ti, (a0, a1)) = items[i]
        for j in range(i + 1, len(items)):
            (tj, (b0, b1)) = items[j]
            if a0 < b1 and b0 < a1:
                clash_partners.setdefault(ti, set()).add(tj)
                clash_partners.setdefault(tj, set()).add(ti)
    for tid in sorted(clash_partners):
        s, e = valid_pins[tid]
        t = targets[tid]
        others = "、".join(
            f"「{targets[o].get('name') or o}」" for o in sorted(clash_partners[tid])
        )
        _err(tid, t, s, e,
             f"目标「{t.get('name') or tid}」的手动固定时段 "
             f"{_iso(grid_start, s)}–{_iso(grid_start, e)} UTC "
             f"与同样手动固定的目标{others}相撞，无法同时保留")
    clash_ids = set(clash_partners)
    for tid in clash_ids:
        valid_pins.pop(tid, None)

    # 4.3 兜底：确认 build_plan 输出里每个固定时段均按原值落盘
    sched = {str(x.get("target_id")): x for x in result.get("scheduled") or []}
    for tid, (s, e) in valid_pins.items():
        rec = sched.get(tid)
        if rec is None or not rec.get("pinned") or \
                int(rec["start_min"]) != s or int(rec["end_min"]) != e:
            _err(tid, targets[tid], s, e,
                 f"目标「{targets[tid].get('name') or tid}」的手动固定时段 "
                 f"{_iso(grid_start, s)}–{_iso(grid_start, e)} UTC 在合并重算后未能保留")
    return errors


# ============================================================ 5) 严格提交
def commit_merge(base_req: dict, supp_req: dict, resolutions) -> dict:
    """执行合并：校验所有拒绝条件 -> 重算 -> 严格固定校验，返回 merged request+result。

    任一不满足都抛 MergeError，不落盘、不产生新版本。
    """
    if not isinstance(resolutions, dict):
        resolutions = {}
    resolutions = {str(k): str(v) for k, v in resolutions.items()}

    analysis = prepare_merge(base_req, supp_req)
    details: dict = {"analysis": analysis}

    if analysis["hard_mismatches"]:
        raise MergeError(
            "两版观测环境不一致，拒绝合并：" + "；".join(
                x["message"] for x in analysis["hard_mismatches"]),
            details,
        )
    if analysis["cal_conflicts"]:
        ids = "、".join(f"「{x['id']}」" for x in analysis["cal_conflicts"])
        raise MergeError(
            f"同 ID 校准定义不同（{ids}），拒绝合并；请先在原版本中统一校准定义",
            details,
        )
    if analysis["cal_overlaps"]:
        ov = analysis["cal_overlaps"][0]
        raise MergeError(
            f"合并后的校准时段相撞：「{ov['a']['label']}」{ov['a']['start']}–{ov['a']['end']} UTC "
            f"与「{ov['b']['label']}」{ov['b']['start']}–{ov['b']['end']} UTC 重叠，拒绝合并",
            details,
        )

    conflict_ids = set(analysis["target_conflict_ids"])
    bad_resolution = {
        tid: side for tid, side in resolutions.items() if side not in SIDES
    }
    if bad_resolution:
        raise MergeError(
            "保留版本取值只能是 'base'（基准版）或 'supplement'（补充版）",
            {**details, "bad_resolutions": bad_resolution},
        )
    extra = sorted(set(resolutions) - conflict_ids)
    if extra:
        raise MergeError(
            f"以下目标在两版中不存在冲突，无需指定保留版本：{'、'.join(extra)}",
            {**details, "unexpected_resolutions": extra},
        )
    unresolved = sorted(conflict_ids - set(resolutions))
    if unresolved:
        raise MergeError(
            f"以下同 ID 目标的整组定义存在冲突，必须逐个指定保留基准版或补充版："
            f"{'、'.join(unresolved)}",
            {**details, "unresolved_targets": unresolved},
        )

    merged_req, chosen_side = _assemble(base_req, supp_req, resolutions, conflict_ids)
    result = build_plan(merged_req)

    pin_errors = _validate_pins_strict(merged_req, result)
    if pin_errors:
        raise MergeError(
            "选中定义中的手动固定时段无法全部保留，合并失败（未保存任何新版本）：\n"
            + "\n".join(f"· {x['reason']}" for x in pin_errors),
            {**details, "pin_errors": pin_errors},
        )

    return {"request": merged_req, "result": result, "chosen_side": chosen_side}
