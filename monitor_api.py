from flask import Blueprint, jsonify, request
from redis.exceptions import RedisError

from detection_api import store, validate_submission
from monitor_store import MonitorBusy, MonitorNotFound, MonitorStore

api = Blueprint("monitors", __name__)


def validate_config(payload, partial=False, current=None):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    required = {"url", "key", "model", "reasoning_effort", "interval_seconds"}
    allowed = required | {"name"}
    if partial:
        allowed.add("enabled")
    if set(payload) - allowed:
        raise ValueError("设置中包含不支持的字段")
    if not partial and not required <= set(payload):
        raise ValueError("请填写 URL、Key、模型、思考强度和检测间隔")
    current = current or {}
    if "name" in payload and (not isinstance(payload["name"], str) or len(payload["name"].strip()) > 80):
        raise ValueError("监测名称最多 80 个字符")
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
        return {**parsed, "name": payload.get("name", "").strip(), "interval_seconds": payload["interval_seconds"]}
    names = {"url": "base_url", "key": "api_key", "model": "model", "reasoning_effort": "reasoning_effort"}
    changes = {("api_key" if key == "key" else key): parsed[value] for key, value in names.items() if key in payload}
    changes.update({key: payload[key] for key in ("interval_seconds", "enabled") if key in payload})
    if "name" in payload:
        changes["name"] = payload["name"].strip()
    return changes


def valid_id(value):
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


@api.get("/status/api/dashboards/default")
def default_dashboard():
    try:
        backend = MonitorStore(store())
        return jsonify(backend.public_dashboard(backend.default_dashboard()))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503


@api.route("/status/api/dashboards", methods=["POST"])
def create_dashboard():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) - {"monitor_id"}:
        return jsonify(error="请求体必须为空对象或包含原监测 ID"), 400
    monitor_id = payload.get("monitor_id")
    if monitor_id is not None and not valid_id(monitor_id):
        return jsonify(error="原监测 ID 无效"), 400
    try:
        backend = MonitorStore(store())
        return jsonify(backend.public_dashboard(backend.create_dashboard(monitor_id))), 201
    except MonitorNotFound as error:
        return jsonify(error=str(error)), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503


@api.route("/status/api/dashboards/<dashboard_id>", methods=["GET"])
@api.route("/status/api/dashboards/<dashboard_id>/monitors", methods=["POST"])
def dashboard(dashboard_id):
    if not valid_id(dashboard_id):
        return jsonify(error="监测面板不存在"), 404
    try:
        backend = MonitorStore(store())
        record = backend.get_dashboard(dashboard_id)
        if not record:
            return jsonify(error="监测面板不存在"), 404
        if request.method == "GET":
            return jsonify(backend.public_dashboard(record))
        payload = request.get_json(silent=True)
        if isinstance(payload, dict) and "source_monitor_id" in payload:
            payload = dict(payload)
            source_id = payload.pop("source_monitor_id")
            if not valid_id(source_id) or source_id not in record["monitor_ids"]:
                return jsonify(error="要复用的连接不属于当前面板"), 404
            source = backend.get(source_id)
            if not source:
                return jsonify(error="要复用的连接不存在"), 404
            if "key" not in payload:
                # Never forward a stored credential to a newly entered URL.
                checked = validate_config({**payload, "key": "placeholder"})
                if checked["base_url"] != source["url"]:
                    raise ValueError("更换接口地址时请重新填写 API Key")
                try:
                    payload["key"] = backend.tasks.cipher.decrypt(source["encrypted_key"].encode()).decode()
                except Exception:
                    raise ValueError("无法读取保存的 Key，请重新填写") from None
        config = validate_config(payload)
        return jsonify(backend.public(backend.add_to_dashboard(dashboard_id, config))), 201
    except MonitorNotFound as error:
        return jsonify(error=str(error)), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503


@api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@api.route("/status/api/monitors/default", methods=["GET", "POST"])
def default_monitor():
    try:
        config = validate_config(request.get_json(silent=True)) if request.method == "POST" else None
        backend = MonitorStore(store())
        record = backend.default(config)
        return jsonify(backend.public(record) if record else None)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except RedisError:
        return jsonify(error="任务存储暂不可用"), 503


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


@api.route("/status/api/monitors/<monitor_id>", methods=["GET", "PATCH", "DELETE"])
@api.route("/status/api/monitors/<monitor_id>/run", methods=["POST"])
def monitor(monitor_id):
    if not valid_id(monitor_id):
        return jsonify(error="监测配置不存在"), 404
    try:
        backend = MonitorStore(store())
        if request.method == "DELETE":
            if not backend.delete(monitor_id):
                return jsonify(error="监测配置不存在"), 404
            return jsonify(id=monitor_id, deleted=True)
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
