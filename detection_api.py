from __future__ import annotations

import hmac
import math
import os

from flask import Blueprint, current_app, jsonify, request
from redis.exceptions import RedisError

from detection_store import DetectionStore, QueueFull, TERMINAL, public_record
from responses_client import responses_url

api = Blueprint("detections", __name__)


def store():
    if "detection_store" not in current_app.extensions:
        current_app.extensions["detection_store"] = DetectionStore()
    return current_app.extensions["detection_store"]


def validate_submission(data):
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if set(data) - {"base_url", "api_key", "model", "temperature"}:
        raise ValueError("仅支持 base_url、api_key、model 和可选 temperature")
    for field in ("base_url", "api_key", "model"):
        if not isinstance(data.get(field), str) or not data[field].strip() or len(data[field]) > 4096:
            raise ValueError(f"{field} 必须是非空字符串，最多 4096 字符")
    if any(ord(c) < 32 or ord(c) > 126 for c in data["api_key"]):
        raise ValueError("api_key 包含无效字符")
    data = dict(data)
    data["base_url"] = responses_url(data["base_url"])
    data["model"] = data["model"].strip()
    temperature = data.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (float, int))
                                    or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("temperature 必须为 0 到 2 之间的数字")
    return data


@api.before_app_request
def authorize_api():
    if not request.path.startswith("/api/"):
        return None
    token = os.getenv("MODELTRACE_API_TOKEN", "")
    required = os.getenv("REQUIRE_API_AUTH", "false").lower() == "true" or request.path.startswith("/api/detections")
    if required and not token:
        return jsonify(error="服务未配置 MODELTRACE_API_TOKEN"), 503
    if token and not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
        return jsonify(error="需要有效的服务访问令牌"), 401
    if os.getenv("ASYNC_ONLY", "false").lower() == "true" and request.method == "POST" and request.path in {
        "/api/test/auto", "/api/test/probe", "/api/enroll/auto", "/api/banks",
    }:
        return jsonify(error="服务器模式使用异步检测 API；指纹库为只读"), 409


@api.after_app_request
def private_responses(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@api.route("/api/detections", methods=["POST"])
def submit():
    try:
        data = validate_submission(request.get_json(silent=True))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    try:
        backend = store()
        backend.reap()
        record = backend.submit(data)
        response = jsonify(public_record(record))
        response.status_code = 202
        response.headers["Location"] = f"/api/detections/{record['id']}"
        response.headers["Retry-After"] = "2"
        return response
    except QueueFull as error:
        return jsonify(error=str(error)), 429, {"Retry-After": "10"}
    except (RedisError, ValueError):
        return jsonify(error="任务存储暂不可用或未配置"), 503


@api.route("/api/detections/<task_id>", methods=["GET", "DELETE"])
def task(task_id):
    if len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
        return jsonify(error="任务不存在或已过期"), 404
    try:
        backend = store()
        backend.reap()
        record = backend.get(task_id)
        if not record:
            return jsonify(error="任务不存在或已过期"), 404
        if request.method == "DELETE" and record["status"] not in TERMINAL:
            record = backend.finish(task_id, "cancelled", "user_cancelled")
        response = jsonify(public_record(record))
        if record["status"] not in TERMINAL:
            response.headers["Retry-After"] = "2"
        return response
    except (RedisError, ValueError):
        return jsonify(error="任务存储暂不可用或未配置"), 503


@api.get("/healthz")
def health():
    try:
        store().redis.ping()
        return jsonify(status="ok")
    except (RedisError, ValueError):
        return jsonify(status="unavailable"), 503
