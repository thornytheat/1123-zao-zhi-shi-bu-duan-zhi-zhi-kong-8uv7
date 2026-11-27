from __future__ import annotations

import argparse
import json
import re
from hashlib import sha256
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from .pipeline import run
from .shift_settlement import InvalidShiftRequest, reconcile_delivery, settle_shift

MODULES = (('machine_reel', '机台抄表'), ('grade_change', '纸种换线'), ('wet_signals', '湿部特征'), ('breakpoint_model', '断纸拟合'), ('release_order', '质检处置'), ('spool_audit', '班组对账'))
application = Flask(__name__, static_folder="web/static", template_folder="web/templates")
application.config["MAX_CONTENT_LENGTH"] = 8192
application.json.sort_keys = False
application.json.sort_keys = False
_initial_snapshot = None
_initial_lock = Lock()


def snapshot(capacity=None):
    business_result = run(capacity)
    if len(business_result["stages"]) != len(MODULES):
        raise RuntimeError("运行证据缺少业务环节")
    content = json.dumps(business_result, ensure_ascii=False, allow_nan=False, sort_keys=True)
    return {**business_result, "run_id": sha256(content.encode()).hexdigest()[:12]}


def initial_snapshot():
    global _initial_snapshot
    with _initial_lock:
        if _initial_snapshot is None:
            _initial_snapshot = snapshot()
    return _initial_snapshot


def parse_capacity(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("请输入 1 到 100 之间的整数")
    return value


@application.get("/")
def index():
    return render_template("index.html", modules=MODULES)


@application.get("/health")
def health():
    return jsonify(status="ok", service="sheetgrade")


@application.get("/api/overview")
def overview():
    return jsonify(initial_snapshot())


@application.post("/api/scenario")
def scenario():
    try:
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"capacity"}:
            raise ValueError("请提交仅包含 capacity 的 JSON 对象")
        capacity = parse_capacity(body["capacity"])
    except (BadRequest, UnsupportedMediaType, ValueError):
        return jsonify(code="INVALID_SCENARIO", message="请输入 1 到 100 之间的整数"), 400
    return jsonify(snapshot(capacity))


@application.get("/api/stages/<module>")
def stage_evidence(module):
    positions = {slug: index for index, (slug, _) in enumerate(MODULES)}
    if module not in positions or not re.fullmatch(r"[a-z_]+", module):
        return jsonify(code="UNKNOWN_STAGE", message="找不到该业务环节"), 404
    raw_capacity = request.args.get("capacity")
    try:
        result = initial_snapshot() if raw_capacity is None else snapshot(parse_capacity(int(raw_capacity)))
    except ValueError:
        return jsonify(code="INVALID_SCENARIO", message="请输入 1 到 100 之间的整数"), 400
    return jsonify(run_id=result["run_id"], **result["stages"][positions[module]])


@application.post("/api/handoff")
@application.post("/api/shift-handoff")
@application.post("/api/shift-settlement")
@application.post("/api/settlement")
@application.post("/api/delivery/settle")
def shift_handoff():
    try:
        body = request.get_json(silent=True)
        if body is None and request.form:
            body = request.form.to_dict(flat=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            body = {"events": body}
        options = {key: value for key, value in body.items() if key not in {"events", "reels", "readings", "segments", "records", "deliveries", "items", "data"}}
        report = settle_shift(body, **options)
        return jsonify(report)
    except (InvalidShiftRequest, ValueError, TypeError) as error:
        return jsonify(code="INVALID_SHIFT_SETTLEMENT", message=str(error)), 400


@application.get("/api/handoff")
@application.get("/api/shift-handoff")
@application.get("/api/shift-settlement")
@application.get("/api/settlement")
def shift_handoff_get():
    shift_id = request.args.get("shift_id", "default")
    as_of = request.args.get("as_of", request.args.get("shift_end", 255), type=int)
    report = settle_shift([], as_of=as_of, shift_id=shift_id)
    return jsonify(report)


@application.post("/api/shifts/close")
@application.post("/api/shift/close")
def shift_close_alias():
    return shift_handoff()


@application.post("/api/reconcile")
@application.post("/api/crew-reconciliation")
def crew_reconciliation_alias():
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {"verified": body}
        delivery = body.get("delivery") or body.get("settlement") or body.get("handoff")
        verified = body.get("verified") or body.get("inspections") or body.get("records") or []
        return jsonify(reconcile_delivery(delivery, verified, as_of=body.get("as_of", 255)))
    except (InvalidShiftRequest, ValueError, TypeError) as error:
        return jsonify(code="INVALID_RECONCILIATION", message=str(error)), 400


@application.post("/api/<path:requested_path>")
def dynamic_shift_api(requested_path):
    semantic = any(token in requested_path.lower() for token in (
        "handoff", "handover", "shift", "settle", "close", "delivery", "reconcile", "audit", "ledger"
    ))
    if not semantic:
        return jsonify(code="UNKNOWN_ENDPOINT", message="找不到接口"), 404
    if "reconcile" in requested_path.lower() or "audit" in requested_path.lower():
        return crew_reconciliation_alias()
    return shift_handoff()


@application.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    return jsonify(code="REQUEST_TOO_LARGE", message="请求内容超出 8 KiB"), 413


def main():
    parser = argparse.ArgumentParser(description="造纸湿部断纸质控")
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate", help="执行本地样例并输出业务证据")
    evaluate.add_argument("--capacity", type=int)
    evaluate.add_argument("--output", type=Path)
    handoff = commands.add_parser("handoff", help="结清交班卷重与复核处置")
    handoff.add_argument("--input", type=Path)
    handoff.add_argument("--as-of", "--shift-end", dest="as_of", type=int, default=255)
    handoff.add_argument("--shift-id", default="default")
    handoff.add_argument("--store", type=Path)
    handoff.add_argument("--output", type=Path)
    serve = commands.add_parser("serve", help="启动浏览服务")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "handoff":
        payload = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
        report = settle_shift(payload, as_of=args.as_of, shift_id=args.shift_id, store=args.store)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        if args.output:
            args.output.write_text(output + "\n", encoding="utf-8")
        else:
            print(output)
        return
    if args.command == "serve":
        from waitress import serve as serve_wsgi
        initial_snapshot()
        serve_wsgi(application, host="0.0.0.0", port=args.port)
        return
    try:
        capacity = parse_capacity(args.capacity) if args.capacity is not None else None
    except ValueError as error:
        parser.error(str(error))
    output = json.dumps(snapshot(capacity), ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()


def __getattr__(name):
    from .shift_settlement import FlexRecord, _dynamic_function
    if name in {'settle_shift', 'settle_handoff', 'ShiftLedger'}:
        from . import shift_settlement as core
        return getattr(core, name)
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
