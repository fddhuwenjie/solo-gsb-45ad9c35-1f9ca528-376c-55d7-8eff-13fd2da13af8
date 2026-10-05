"""Flask 应用：静态页面 + 计算/版本/导出 API，SQLite 持久化。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

from .merge import MergeConflict, prepare_merge
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


def _load_version_row(conn: sqlite3.Connection, vid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, label, request, result, created_at FROM versions WHERE id = ?",
        (vid,),
    ).fetchone()


def _version_from_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "request": json.loads(row["request"]),
        "result": json.loads(row["result"]),
    }


@app.get("/api/versions/<vid>")
def get_version(vid):
    conn = get_db()
    try:
        row = _load_version_row(conn, vid)
    finally:
        conn.close()
    if row is None:
        return jsonify(error="版本不存在"), 404
    return jsonify(_version_from_row(row))


@app.post("/api/versions/merge")
def merge_versions():
    """合并两份已保存版本；dry-run 预检，save=true 时一次性写入新版本。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    base_id = str(body.get("base_version_id") or "").strip()
    supp_id = str(body.get("supplement_version_id") or "").strip()
    if not base_id or not supp_id:
        return jsonify(error="必须提供 base_version_id 和 supplement_version_id"), 400
    if base_id == supp_id:
        return jsonify(error="基准版和补充版必须是两个不同的已保存版本"), 400
    resolutions = body.get("target_resolutions") or {}
    if not isinstance(resolutions, dict):
        return jsonify(error="target_resolutions 必须是 JSON 对象"), 400
    save = body.get("save", False)
    if not isinstance(save, bool):
        return jsonify(error="save 必须是布尔值"), 400
    label = str(body.get("label") or "").strip()

    conn = get_db()
    try:
        base_row = _load_version_row(conn, base_id)
        supp_row = _load_version_row(conn, supp_id)
    finally:
        conn.close()
    missing = [
        name for name, row in (("基准版", base_row), ("补充版", supp_row)) if row is None
    ]
    if missing:
        return jsonify(error="、".join(missing) + "不存在"), 404

    base_version = _version_from_row(base_row)
    supp_version = _version_from_row(supp_row)
    try:
        merged = prepare_merge(base_version, supp_version, resolutions)
    except MergeConflict as exc:
        return jsonify(error=str(exc), merge_conflicts=exc.details), 400
    except PlanError as exc:
        return jsonify(error=str(exc)), 400

    merged_request = merged["merged_request"]
    result = merged["result"]
    out = {
        "base_version_id": base_id,
        "base_label": base_version["label"],
        "supplement_version_id": supp_id,
        "supplement_label": supp_version["label"],
        "merged_request": merged_request,
        "selected_side": merged["selected_side"],
        "schedule": result["scheduled"],
        "unscheduled": result["unscheduled"],
        "result": result,
    }

    if not save:
        out["saved"] = False
        return jsonify(out)

    label = label or f"合并：{base_version['label']} ＋ {supp_version['label']}"
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
                    json.dumps(merged_request, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    created,
                ),
            )
            conn.commit()
    finally:
        conn.close()

    out.update(
        {
            "saved": True,
            "id": vid,
            "label": label,
            "created_at": created,
        }
    )
    return jsonify(out)


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
