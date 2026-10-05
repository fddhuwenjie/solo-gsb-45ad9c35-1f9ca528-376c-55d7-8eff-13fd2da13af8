"""Flask 应用：静态页面 + 计算/版本/导出 API，SQLite 持久化。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

from .merging import MergeError, commit_merge, prepare_merge
from .planner import build_plan
from .scheduling_utils import PlanError
from .verification import verify_exposures

DB_PATH = os.environ.get(
    "PLANNER_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "planner.db"),
)
_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()


# ------------------------------------------------------------------ database
def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), 0o775, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id          TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            request     TEXT NOT NULL,
            result      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(_STATIC, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(_STATIC, filename)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


# ------------------------------------------------------------------- compute
@app.post("/api/compute")
def api_compute():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):

        return jsonify(error="请求体必须是 JSON 对象"), 400
    try:
        result = build_plan(payload)
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


# ----------------------------------------------------------------- versions
@app.get("/api/versions")
def list_versions():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM versions ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return jsonify(
        [
            {"id": r["id"], "label": r["label"], "created_at": r["created_at"]}
            for r in rows
        ]
    )


@app.post("/api/versions")
def save_version():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
        return jsonify(error="需要包含 request（计算输入）的 JSON 对象"), 400
    label = str(body.get("label") or "").strip()
    try:
        result = build_plan(body["request"])
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    label = label or datetime.now(timezone.utc).strftime("版本 %Y-%m-%d %H:%M UTC")
    vid = uuid.uuid4().hex[:12]
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_db()
    try:
        with _lock:
            conn.execute(
                "INSERT INTO versions (id, label, request, result, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    vid,
                    label,
                    json.dumps(body["request"], ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    created,
                ),
            )
            conn.commit()
    finally:
        conn.close()
    return jsonify({"id": vid, "label": label, "created_at": created, "result": result})


@app.get("/api/versions/<vid>")
def get_version(vid):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, label, request, result, created_at FROM versions WHERE id = ?",
            (vid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify(error="版本不存在"), 404
    return jsonify(
        {
            "id": row["id"],
            "label": row["label"],
            "created_at": row["created_at"],
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]),
        }
    )


@app.delete("/api/versions/<vid>")
def delete_version(vid):
    conn = get_db()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM versions WHERE id = ?", (vid,))
            conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        return jsonify(error="版本不存在"), 404
    return jsonify(deleted=vid)


# ------------------------------------------------------------------- merging
BASE_SIDE = "base"
SUPP_SIDE = "supplement"


def _load_two_versions(conn, base_id, supp_id):
    """按 id 取两份版本的 request；任一不存在抛 MergeError（details 含缺失 id）。"""
    missing = []
    reqs = {}
    for key, vid in ((BASE_SIDE, base_id), (SUPP_SIDE, supp_id)):
        if not isinstance(vid, str) or not vid:
            raise MergeError("必须指定基准版与补充版的版本 id", {"missing": [key]})
        row = conn.execute(
            "SELECT request FROM versions WHERE id = ?", (vid,)
        ).fetchone()
        if row is None:
            missing.append(vid)
        else:
            reqs[key] = json.loads(row["request"])
    if missing:
        raise MergeError(
            f"版本不存在：{', '.join(missing)}", {"missing_versions": missing}
        )
    return reqs[BASE_SIDE], reqs[SUPP_SIDE]


@app.post("/api/versions/merge/prepare")
def merge_prepare():
    """预检两版合并：返回环境比对、同 ID 目标冲突与校准冲突（只读，不落盘）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    conn = get_db()
    try:
        try:
            base_req, supp_req = _load_two_versions(
                conn, body.get("base_version_id"), body.get("supplement_version_id")
            )
        except MergeError as exc:
            return jsonify(error=str(exc), details=exc.details), 404
    finally:
        conn.close()
    if body.get("base_version_id") == body.get("supplement_version_id"):
        return jsonify(error="基准版与补充版不能是同一个版本"), 400
    try:
        analysis = prepare_merge(base_req, supp_req)
    except MergeError as exc:
        return jsonify(error=str(exc), details=exc.details), 400
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    analysis["base_version_id"] = body.get("base_version_id")
    analysis["supplement_version_id"] = body.get("supplement_version_id")
    return jsonify(analysis)


@app.post("/api/versions/merge")
def merge_versions():
    """严格合并两版并一次性写入新版本；任何冲突都整体失败，原版本保持不变。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    base_id = body.get("base_version_id")
    supp_id = body.get("supplement_version_id")
    if not isinstance(base_id, str) or not isinstance(supp_id, str) or not base_id \
            or not supp_id:
        return jsonify(error="必须提供 base_version_id 与 supplement_version_id"), 400
    if base_id == supp_id:
        return jsonify(error="基准版与补充版不能是同一个版本"), 400

    conn = get_db()
    try:
        try:
            base_req, supp_req = _load_two_versions(conn, base_id, supp_id)
        except MergeError as exc:
            return jsonify(error=str(exc), details=exc.details), 404

        # 合并前先取出两份版本的标签用于默认命名（此时尚未做任何写操作）
        brow = conn.execute("SELECT label FROM versions WHERE id = ?", (base_id,)).fetchone()
        srow = conn.execute("SELECT label FROM versions WHERE id = ?", (supp_id,)).fetchone()
    finally:
        conn.close()

    try:
        merged = commit_merge(base_req, supp_req, body.get("resolutions"))
    except MergeError as exc:
        return jsonify(error=str(exc), details=exc.details), 409
    except PlanError as exc:
        return jsonify(error=str(exc)), 400

    label = str(body.get("label") or "").strip()
    label = label or (
        f"合并：{(brow['label'] if brow else base_id)} + {(srow['label'] if srow else supp_id)}"
    )[:120]
    vid = uuid.uuid4().hex[:12]
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_db()
    try:
        with _lock:
            conn.execute(
                "INSERT INTO versions (id, label, request, result, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    vid,
                    label,
                    json.dumps(merged["request"], ensure_ascii=False),
                    json.dumps(merged["result"], ensure_ascii=False),
                    created,
                ),
            )
            conn.commit()
    finally:
        conn.close()
    return jsonify({
        "id": vid,
        "label": label,
        "created_at": created,
        "result": merged["result"],
        "request": merged["request"],
        "chosen_side": merged["chosen_side"],
        "sources": {"base": base_id, "supplement": supp_id},
    })


# ----------------------------------------------------------------- verify
@app.post("/api/versions/<vid>/verify")
def verify_version(vid):
    """实测曝光核验：对照已保存版本核对实际观测时段（只读，不改动版本）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, label, result FROM versions WHERE id = ?", (vid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify(error="版本不存在"), 404
    try:
        out = verify_exposures(json.loads(row["result"]), body.get("records"))
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    out["version_id"] = row["id"]
    out["label"] = row["label"]
    return jsonify(out)


# -------------------------------------------------------------------- export
@app.post("/api/export")
def api_export():
    """返回自描述的时间表 JSON（输入参数 + 排程结果）。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    try:
        result = build_plan(payload)
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        {
            "schema": "observation-schedule/v1",
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "request": payload,
            "schedule": result,
        }
    )


if __name__ == "__main__":
    # 直接 python -m app.main 时使用；容器内由 gunicorn 启动
    get_db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
