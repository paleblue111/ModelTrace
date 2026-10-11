from flask import Blueprint, jsonify, request
from redis.exceptions import RedisError

from detection_api import store, validate_submission
from monitor_store import MonitorBusy, MonitorStore

api = Blueprint("monitors", __name__)


def validate_config(payload, partial=False, current=None):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = {"url", "key", "model", "reasoning_effort", "interval_seconds"}
    if partial:
        allowed.add("enabled")
    if set(payload) - allowed:
        raise ValueError("设置中包含不支持的字段")
    if not partial and set(payload) != allowed:
        raise ValueError("请填写 URL、Key、模型、思考强度和检测间隔")
    current = current or {}
    # Reuse the four-field API's validation without exposing stored credentials.
    parsed = {}
    if not partial or set(payload) & {"url", "key", "model", "reasoning_effort"}:
        parsed = validate_submission({"base_url": payload.get("url", current.get("url", "https://example.com/v1")),
                                     "api_key": payload.get("key", "placeholder"),
                                     "model": payload.get("model", "placeholder"),
                                     "reasoning_effort": payload.get("reasoning_effort", "medium")})
    if "reasoning_effort" in payload and payload["reasoning_effort"] is None:
        raise ValueError("请选择思考强度")
    if "interval_seconds" in payload:
        value = payload["interval_seconds"]
        if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 604800:
            raise ValueError("检测间隔必须为 1 分钟到 7 天")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise ValueError("enabled 必须为布尔值")
    if not partial:
        return {**parsed, "interval_seconds": payload["interval_seconds"]}
    names = {"url": "base_url", "key": "api_key", "model": "model", "reasoning_effort": "reasoning_effort"}
    changes = {("api_key" if key == "key" else key): parsed[value] for key, value in names.items() if key in payload}
    changes.update({key: payload[key] for key in ("interval_seconds", "enabled") if key in payload})
    return changes


@api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@api.route("/status/api/monitors", methods=["POST"])
def create():
    try:
        config = validate_config(request.get_json(silent=True))
        backend = MonitorStore(store())
        return jsonify(backend.public(backend.create(config))), 201
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503


@api.route("/status/api/monitors/<monitor_id>", methods=["GET", "PATCH"])
@api.route("/status/api/monitors/<monitor_id>/run", methods=["POST"])
def monitor(monitor_id):
    if len(monitor_id) != 32 or any(c not in "0123456789abcdef" for c in monitor_id):
        return jsonify(error="监测配置不存在"), 404
    try:
        backend = MonitorStore(store())
        if request.method == "PATCH":
            current = backend.get(monitor_id)
            if not current:
                return jsonify(error="监测配置不存在"), 404
            record = backend.update(monitor_id, validate_config(request.get_json(silent=True), partial=True, current=current))
        elif request.method == "POST":
            record = backend.update(monitor_id, {"run_requested": True})
        else:
            record = backend.get(monitor_id)
        if not record:
            return jsonify(error="监测配置不存在"), 404
        return jsonify(backend.public(record))
    except MonitorBusy as error:
        return jsonify(error=str(error)), 409
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503
