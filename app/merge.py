"""两份已保存观测版本的严格合并。

合并规则：
- 观测点、UTC 日期范围、暮光/夜间、最低高度、最大空气质量必须一致；
- 目标按 ID 并集去重；同 ID 定义不同（含手动固定时段）时，必须逐个选择
  ``base`` 或 ``supplement`` 的整组定义；
- 校准按 ID 并集去重；同 ID 校准定义不同、或合并后校准区间重叠，直接拒绝；
- 合并后从零重算全部窗口和排程，并严格保留所选手动固定时段。固定时段越界、
  因目标定义变化而离开可行窗口、固定时段互相重叠或占用校准时，整次合并失败。
"""

from __future__ import annotations

import copy
import math
from datetime import timedelta

from .astronomy import TWILIGHT_MODES, PlanError
from .planner import build_plan
from .scheduling_utils import parse_coord, parse_dec, parse_ra, parse_utc


class MergeConflict(PlanError):
    """带结构化明细的合并冲突；路由把 details 原样返回给调用方。"""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


# --------------------------------------------------------------------- 工具
def _iso_minute(grid_start, minute: int) -> str:
    return (grid_start + timedelta(minutes=int(minute))).strftime("%Y-%m-%dT%H:%MZ")


def _iso_dt(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_details() -> dict:
    return {
        "context_mismatches": [],
        "target_conflicts": [],
        "missing_resolutions": [],
        "invalid_resolutions": [],
        "calibration_conflicts": [],
        "calibration_collisions": [],
        "pin_errors": [],
    }


def _raise(details: dict, message: str) -> None:
    counts = {
        "context_mismatches": "观测上下文不一致",
        "target_conflicts": "同 ID 目标定义冲突",
        "missing_resolutions": "尚未选择保留版本的目标冲突",
        "invalid_resolutions": "非法的目标冲突选择",
        "calibration_conflicts": "同 ID 校准定义冲突",
        "calibration_collisions": "合并后校准时段相撞",
        "pin_errors": "手动固定时段无法保留",
    }
    problems = [label for key, label in counts.items() if details.get(key)]
    if not problems:
        problems = [message]
    raise MergeConflict("拒绝合并：" + "；".join(problems), details)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)


# ------------------------------------------------------------- 上下文规范化
def _canon_context(req: dict) -> dict:
    site = req.get("site") if isinstance(req.get("site"), dict) else {}
    lat = parse_coord(site.get("lat"), "lat")
    lon = parse_coord(site.get("lon"), "lon")

    start_date = parse_utc(req.get("start_date")).date().isoformat()
    end_date = parse_utc(req.get("end_date"), end_of_day=True).date().isoformat()

    mode = str(req.get("twilight_mode", "night"))
    if mode not in TWILIGHT_MODES:
        raise PlanError(f"未知暮光模式：{mode}")
    try:
        min_alt = float(req.get("min_altitude", 20.0))
        max_x = float(req.get("max_airmass", 2.5))
    except (TypeError, ValueError):
        raise PlanError("最低高度 / 最大空气质量必须是数字")
    if not all(math.isfinite(x) for x in (lat, lon, min_alt, max_x)):
        raise PlanError("观测点或约束不能是 NaN/Infinity")

    return {
        "site": {"lat": lat, "lon": lon},
        "start_date": start_date,
        "end_date": end_date,
        "twilight_mode": mode,
        "min_altitude": min_alt,
        "max_airmass": max_x,
    }


def _compare_context(base: dict, supp: dict) -> list[dict]:
    cb, cs = _canon_context(base), _canon_context(supp)
    out: list[dict] = []
    if not _close(cb["site"]["lat"], cs["site"]["lat"]) or not _close(
        cb["site"]["lon"], cs["site"]["lon"]
    ):
        out.append(
            {
                "field": "site",
                "label": "观测点（纬度/经度）",
                "base": cb["site"],
                "supplement": cs["site"],
            }
        )
    for field, label in (
        ("start_date", "UTC 开始日期"),
        ("end_date", "UTC 结束日期"),
        ("twilight_mode", "夜间/暮光条件"),
        ("min_altitude", "最低高度"),
        ("max_airmass", "最大空气质量"),
    ):
        b, s = cb[field], cs[field]
        equal = _close(b, s) if isinstance(b, float) else b == s
        if not equal:
            out.append(
                {"field": field, "label": label, "base": b, "supplement": s}
            )
    return out


# --------------------------------------------------------------- 目标规范化
def _pins_dict(req: dict) -> dict:
    pins = req.get("pins")
    return pins if isinstance(pins, dict) else {}


def _canon_pin(raw, target_id: str) -> dict | None:
    """pins 中一个目标的手动固定起点；None 表示未固定。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return {"valid": False, "start_min": None}
    try:
        value = float(raw.get("start_min"))
    except (TypeError, ValueError):
        return {"valid": False, "start_min": None}
    if not math.isfinite(value) or int(value) != value:
        return {"valid": False, "start_min": None}
    return {"valid": True, "start_min": int(value)}


def _canon_target(raw: dict, index: int, pins_raw: dict) -> dict:
    tid = str(raw.get("id") or f"T{index + 1}").strip()
    name = str(raw.get("name") or tid).strip() or tid
    ra = parse_ra(raw.get("ra"))
    dec = parse_dec(raw.get("dec"))
    try:
        # 与 build_plan 保持一致：int(float(...))
        exposure = int(float(raw.get("exposure_minutes")))
    except (TypeError, ValueError):
        raise PlanError(f"目标「{name}」的曝光时长不是数字")
    try:
        # 与 build_plan 保持一致：可经 JSON 存入数字后再统一转整数。
        priority = int(float(raw.get("priority", 100)))
    except (TypeError, ValueError):
        raise PlanError(f"目标「{name}」的优先级不是整数")
    return {
        "id": tid,
        "name": name,
        "ra": raw.get("ra"),
        "dec": raw.get("dec"),
        "ra_deg": ra,
        "dec_deg": dec,
        "exposure_minutes": exposure,
        "priority": priority,
        "pin": _canon_pin(pins_raw.get(tid), tid),
    }


def _index_targets(req: dict) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
    raw_pins = _pins_dict(req)
    order: list[str] = []
    raw_by_id: dict[str, dict] = {}
    canon_by_id: dict[str, dict] = {}
    for i, raw in enumerate(req.get("targets") or []):
        if not isinstance(raw, dict):
            raise PlanError(f"第 {i + 1} 个目标不是 JSON 对象")
        canon = _canon_target(raw, i, raw_pins)
        tid = canon["id"]
        if tid in canon_by_id:
            raise PlanError(f"目标 id 重复：{tid!r}")
        order.append(tid)
        raw_by_id[tid] = raw
        canon_by_id[tid] = canon
    return order, raw_by_id, canon_by_id


def _pin_key(canon: dict):
    pin = canon["pin"]
    if pin is None:
        return None
    if not pin["valid"]:
        return ("invalid",)
    return ("valid", pin["start_min"])


def _targets_differ(a: dict, b: dict) -> list[str]:
    diffs = []
    if a["name"] != b["name"]:
        diffs.append("name")
    if not _close(a["ra_deg"], b["ra_deg"]):
        diffs.append("ra_deg")
    if not _close(a["dec_deg"], b["dec_deg"]):
        diffs.append("dec_deg")
    if a["exposure_minutes"] != b["exposure_minutes"]:
        diffs.append("exposure_minutes")
    if a["priority"] != b["priority"]:
        diffs.append("priority")
    if _pin_key(a) != _pin_key(b):
        diffs.append("pin")
    return diffs


def _target_side_summary(canon: dict, grid_start) -> dict:
    pin = canon["pin"]
    pin_start = None
    pin_minute = None
    pin_present = pin is not None
    pin_valid = None
    if pin is not None:
        pin_valid = bool(pin["valid"])
        if pin_valid:
            pin_minute = pin["start_min"]
            pin_start = _iso_minute(grid_start, pin_minute)
    return {
        "name": canon["name"],
        "ra": canon["ra"],
        "dec": canon["dec"],
        "ra_deg": round(canon["ra_deg"], 6),
        "dec_deg": round(canon["dec_deg"], 6),
        "exposure_minutes": canon["exposure_minutes"],
        "priority": canon["priority"],
        "pin_present": pin_present,
        "pin_valid": pin_valid,
        "pin_start_min": pin_minute,
        "pin_start": pin_start,
    }


# --------------------------------------------------------------- 校准规范化
def _canon_calibration(raw: dict, index: int) -> dict:
    cid = str(raw.get("id") or f"C{index + 1}").strip() or f"C{index + 1}"
    label = str(raw.get("label") or f"校准 {index + 1}").strip() or f"校准 {index + 1}"
    start = parse_utc(raw.get("start"))
    end = parse_utc(raw.get("end"))
    if end <= start:
        raise PlanError(f"校准「{label}」结束时间须晚于开始时间")
    return {
        "id": cid,
        "label": label,
        "start_dt": start,
        "end_dt": end,
        "start": _iso_dt(start),
        "end": _iso_dt(end),
    }


def _cal_equal(a: dict, b: dict) -> bool:
    return (
        a["id"] == b["id"]
        and a["label"] == b["label"]
        and a["start_dt"] == b["start_dt"]
        and a["end_dt"] == b["end_dt"]
    )


def _cal_summary(c: dict) -> dict:
    return {
        "id": c["id"],
        "label": c["label"],
        "start": c["start"],
        "end": c["end"],
    }


def _index_calibrations(req: dict, side: str) -> tuple[list[str], dict[str, dict], list[dict]]:
    order: list[str] = []
    by_id: dict[str, dict] = {}
    conflicts: list[dict] = []
    for i, raw in enumerate(req.get("calibrations") or []):
        if not isinstance(raw, dict):
            raise PlanError(f"{side}第 {i + 1} 个校准不是 JSON 对象")
        c = _canon_calibration(raw, i)
        cid = c["id"]
        if cid in by_id:
            old = by_id[cid]
            if not _cal_equal(old, c):
                conflicts.append(
                    {
                        "calibration_id": cid,
                        "base": _cal_summary(old) if side == "基准版" else None,
                        "supplement": _cal_summary(c) if side == "补充版" else None,
                        "reason": f"{side}内同 ID 校准重复且定义不同",
                    }
                )
            continue
        order.append(cid)
        by_id[cid] = c
    return order, by_id, conflicts


def _clip_calibration(c: dict, grid_start, n: int) -> tuple[int, int] | None:
    s = int((c["start_dt"] - grid_start).total_seconds() // 60)
    e = int((c["end_dt"] - grid_start).total_seconds() // 60)
    s = max(0, s)
    e = min(n, e)
    return (s, e) if e > s else None


def _cal_collisions(
    cal_order: list[str],
    cal_by_id: dict[str, dict],
    grid_start,
    n: int,
) -> list[dict]:
    clipped = []
    for cid in cal_order:
        iv = _clip_calibration(cal_by_id[cid], grid_start, n)
        if iv is not None:
            clipped.append((cid, cal_by_id[cid], iv[0], iv[1]))

    collisions = []
    for i in range(len(clipped)):
        cid1, c1, a1, b1 = clipped[i]
        for j in range(i + 1, len(clipped)):
            cid2, c2, a2, b2 = clipped[j]
            if a1 < b2 and a2 < b1:
                collisions.append(
                    {
                        "calibration_id1": cid1,
                        "label1": c1["label"],
                        "start1": _iso_minute(grid_start, a1),
                        "end1": _iso_minute(grid_start, b1),
                        "calibration_id2": cid2,
                        "label2": c2["label"],
                        "start2": _iso_minute(grid_start, a2),
                        "end2": _iso_minute(grid_start, b2),
                        "reason": "合并后的固定校准时段互相重叠",
                    }
                )
    return collisions


# ------------------------------------------------------------- 固定时段核验
def _validate_pins(merged_request: dict, result: dict) -> list[dict]:
    grid_start = parse_utc((result.get("grid") or {}).get("start"))
    n = int((result.get("grid") or {}).get("minutes") or 0)
    targets = {str(t.get("id")): t for t in result.get("targets") or []}
    pins_in = _pins_dict(merged_request)

    errors: list[dict] = []
    valid: dict[str, dict] = {}
    out_of_range: set[str] = set()

    def add_error(tid: str, name: str, reason: str, **extra):
        rec = {
            "target_id": tid,
            "name": name,
            "start_min": None,
            "end_min": None,
            "start": None,
            "end": None,
            "reason": reason,
        }
        rec.update(extra)
        errors.append(rec)

    for tid, raw_pin in pins_in.items():
        t = targets.get(str(tid))
        if t is None:
            add_error(str(tid), str(tid), "手动固定时段所属的目标不在合并计划中")
            continue
        name = t.get("name") or tid
        pin = _canon_pin(raw_pin, tid)
        if pin is None:
            continue
        if not pin["valid"]:
            add_error(tid, name, "手动固定时段定义无效：必须是包含整数 start_min 的对象")
            continue
        s = pin["start_min"]
        e = s + int(t.get("exposure_minutes") or 0)
        rec = {
            "target_id": tid,
            "name": name,
            "start_min": s,
            "end_min": e,
            "start": _iso_minute(grid_start, s),
            "end": _iso_minute(grid_start, e),
        }
        valid[tid] = rec
        if s < 0 or e > n:
            out_of_range.add(tid)
            add_error(
                tid,
                name,
                f"手动固定时段 {rec['start']}–{rec['end']} 超出共同 UTC 日期范围",
                **{k:v for k,v in rec.items() if k not in ("target_id","name")},
            )

    # 固定时段彼此相撞：双方都必须指出。
    pair_blocked: set[str] = set()
    items = list(valid.values())
    for i in range(len(items)):
        a = items[i]
        for j in range(i + 1, len(items)):
            b = items[j]
            if a["start_min"] < b["end_min"] and b["start_min"] < a["end_min"]:
                pair_blocked.update((a["target_id"], b["target_id"]))
                add_error(
                    a["target_id"],
                    a["name"],
                    f"手动固定时段 {a['start']}–{a['end']} 与另一手动固定目标"
                    f"「{b['name']}」{b['start']}–{b['end']} 相撞",
                    **{k: v for k, v in a.items() if k not in ("target_id", "name")},
                    other_target_id=b["target_id"],
                    other_target_name=b["name"],
                )
                add_error(
                    b["target_id"],
                    b["name"],
                    f"手动固定时段 {b['start']}–{b['end']} 与另一手动固定目标"
                    f"「{a['name']}」{a['start']}–{a['end']} 相撞",
                    **{k: v for k, v in b.items() if k not in ("target_id", "name")},
                    other_target_id=a["target_id"],
                    other_target_name=a["name"],
                )

    # 占用校准。
    cal_blocked: set[str] = set()
    for tid, rec in valid.items():
        if tid in pair_blocked:
            continue
        for c in result.get("calibrations") or []:
            cs, ce = int(c["start_min"]), int(c["end_min"])
            if rec["start_min"] < ce and cs < rec["end_min"]:
                cal_blocked.add(tid)
                add_error(
                    tid,
                    rec["name"],
                    f"手动固定时段 {rec['start']}–{rec['end']} 被合并后校准"
                    f"「{c.get('label')}」{c.get('start')}–{c.get('end')} 占用",
                    **{k:v for k,v in rec.items() if k not in ("target_id","name")},
                    calibration_id=c.get("id"),
                    calibration_label=c.get("label"),
                )

    # 目标定义变化后，固定时段必须仍整体落在重算出的可行窗口中。
    window_blocked: set[str] = set()
    for tid, rec in valid.items():
        if tid in pair_blocked or tid in cal_blocked or tid in out_of_range:
            continue
        t = targets[tid]
        windows = [[int(w["start_min"]), int(w["end_min"])] for w in t.get("windows") or []]
        if not any(a <= rec["start_min"] and rec["end_min"] <= b for a, b in windows):
            window_blocked.add(tid)
            add_error(
                tid,
                rec["name"],
                f"按合并后的目标「{rec['name']}」定义重算，固定时段 "
                f"{rec['start']}–{rec['end']} 无法整体落入任何可行窗口"
                "（夜间/高度/空气质量约束不再同时满足）",
                **{k:v for k,v in rec.items() if k not in ("target_id","name")},
            )

    # 最后兜底：确保 build_plan 没有以 warning 形式悄悄丢掉任何固定时段。
    scheduled = {
        str(s.get("target_id")): s for s in (result.get("scheduled") or [])
    }
    for tid, rec in valid.items():
        if tid in pair_blocked or tid in cal_blocked or tid in window_blocked or tid in out_of_range:
            continue
        s = scheduled.get(tid)
        if (
            s is None
            or not s.get("pinned")
            or int(s.get("start_min", -1)) != rec["start_min"]
            or int(s.get("end_min", -1)) != rec["end_min"]
        ):
            add_error(
                tid,
                rec["name"],
                f"重算后无法保留手动固定时段 {rec['start']}–{rec['end']}",
                **{k:v for k,v in rec.items() if k not in ("target_id","name")},
            )

    errors.sort(key=lambda x: (x.get("start_min") is None, x.get("start_min") or 0, x["target_id"]))
    return errors


# ------------------------------------------------------------------- 主入口
def prepare_merge(
    base_version: dict,
    supplement_version: dict,
    resolutions: dict,
) -> dict:
    """检查并构造合并请求；成功时同时返回已重算的 result。不写数据库。"""
    details = _empty_details()
    base_req = base_version["request"]
    supp_req = supplement_version["request"]
    if not isinstance(base_req, dict) or not isinstance(supp_req, dict):
        raise PlanError("版本缺少有效的计算输入 request")
    if base_version.get("id") == supplement_version.get("id"):
        raise PlanError("基准版和补充版必须是两个不同的已保存版本")

    details["context_mismatches"] = _compare_context(base_req, supp_req)
    if details["context_mismatches"]:
        _raise(details, "两版观测点、日期范围或观测约束不一致")

    # 上下文已经证明一致，下面使用基准版的规范化值构造公共时间网格与约束。
    base_ctx = _canon_context(base_req)
    grid_start = parse_utc(base_req.get("start_date")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    grid_end = parse_utc(base_req.get("end_date"), end_of_day=True).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    n = int((grid_end - grid_start).total_seconds() // 60)

    base_order, base_raw, base_targets = _index_targets(base_req)
    supp_order, supp_raw, supp_targets = _index_targets(supp_req)

    # 同 ID 目标定义差异。
    conflicting_ids: list[str] = []
    for tid in base_order:
        if tid in supp_targets:
            diffs = _targets_differ(base_targets[tid], supp_targets[tid])
            if diffs:
                conflicting_ids.append(tid)
                details["target_conflicts"].append(
                    {
                        "target_id": tid,
                        "differences": diffs,
                        "base": _target_side_summary(base_targets[tid], grid_start),
                        "supplement": _target_side_summary(
                            supp_targets[tid], grid_start
                        ),
                    }
                )

    if not isinstance(resolutions, dict):
        raise PlanError("target_resolutions 必须是 JSON 对象")
    conflict_set = set(conflicting_ids)
    for tid in conflicting_ids:
        choice = resolutions.get(tid)
        if choice is None:
            details["missing_resolutions"].append(tid)
        elif choice not in ("base", "supplement"):
            details["invalid_resolutions"].append(
                {"target_id": tid, "supplied": choice}
            )
    for tid in resolutions:
        if tid not in conflict_set:
            details["invalid_resolutions"].append(
                {"target_id": tid, "supplied": resolutions[tid], "reason": "该目标没有冲突"}
            )

    # 校准：按 ID 去重，定义必须完全相同。
    base_cal_order, base_cals, base_cal_dupes = _index_calibrations(base_req, "基准版")
    supp_cal_order, supp_cals, supp_cal_dupes = _index_calibrations(supp_req, "补充版")
    details["calibration_conflicts"].extend(base_cal_dupes)
    details["calibration_conflicts"].extend(supp_cal_dupes)
    for cid in base_cal_order:
        if cid in supp_cals and not _cal_equal(base_cals[cid], supp_cals[cid]):
            details["calibration_conflicts"].append(
                {
                    "calibration_id": cid,
                    "base": _cal_summary(base_cals[cid]),
                    "supplement": _cal_summary(supp_cals[cid]),
                    "reason": "同 ID 校准定义不同",
                }
            )

    cal_order = list(base_cal_order)
    cal_by_id = dict(base_cals)
    for cid in supp_cal_order:
        if cid not in cal_by_id:
            cal_order.append(cid)
            cal_by_id[cid] = supp_cals[cid]
    details["calibration_collisions"] = _cal_collisions(
        cal_order, cal_by_id, grid_start, n
    )

    if (
        details["missing_resolutions"]
        or details["invalid_resolutions"]
        or details["calibration_conflicts"]
        or details["calibration_collisions"]
    ):
        _raise(details, "合并仍有未解决冲突")

    # ----------------------------------------------------------- 构造合并输入
    selected_side: dict[str, str] = {}
    merged_targets = []
    merged_pins = {}

    def take_target(tid: str, side: str):
        selected_side[tid] = side
        if side == "base":
            raw_req, raw_pins = base_req, base_raw
        else:
            raw_req, raw_pins = supp_req, supp_raw
        merged_targets.append(copy.deepcopy(raw_pins[tid]))
        pin_raw = _pins_dict(raw_req).get(tid)
        if pin_raw is not None:
            merged_pins[tid] = copy.deepcopy(pin_raw)

    for tid in base_order:
        if tid in supp_targets:
            take_target(tid, resolutions.get(tid, "base"))
        else:
            take_target(tid, "base")
    for tid in supp_order:
        if tid not in base_targets:
            take_target(tid, "supplement")

    # 直接保留按顺序选出的原始校准记录。
    merged_calibrations: list[dict] = []
    raw_cal_records: dict[tuple[str, str], dict] = {}
    for i, c in enumerate(base_req.get("calibrations") or []):
        cid = _canon_calibration(c, i)["id"]
        raw_cal_records.setdefault(("base", cid), c)
    for i, c in enumerate(supp_req.get("calibrations") or []):
        cid = _canon_calibration(c, i)["id"]
        raw_cal_records.setdefault(("supplement", cid), c)
    for cid in cal_order:
        side = "base" if cid in base_cals else "supplement"
        merged_calibrations.append(copy.deepcopy(raw_cal_records[(side, cid)]))

    merged_request = {
        "site": {
            "lat": base_ctx["site"]["lat"],
            "lon": base_ctx["site"]["lon"],
        },
        "start_date": base_ctx["start_date"],
        "end_date": base_ctx["end_date"],
        "twilight_mode": base_ctx["twilight_mode"],
        "min_altitude": base_ctx["min_altitude"],
        "max_airmass": base_ctx["max_airmass"],
        "targets": merged_targets,
        "calibrations": merged_calibrations,
        "pins": merged_pins,
    }

    # 全部窗口与排程从零重算；随后严格核验固定时段，不允许靠 warning 忽略。
    result = build_plan(merged_request)
    details["pin_errors"] = _validate_pins(merged_request, result)
    if details["pin_errors"]:
        _raise(details, "选中的手动固定时段无法保留")

    return {
        "merged_request": merged_request,
        "result": result,
        "selected_side": selected_side,
    }
