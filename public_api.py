"""Read-only publication projections and anonymous, one-shot website detections."""
import json
import time
import uuid

from flask import Blueprint, jsonify, request
from redis import WatchError
from redis.exceptions import RedisError

from detection_api import store, validate_submission
from detection_store import PUBLIC_EXPIRY, PUBLIC_INDEX, PUBLIC_VISIBLE_INDEX, PublicDisabled, QueueFull
from monitor_api import valid_id
from monitor_store import MonitorStore
from site_settings import SiteSettings

api = Blueprint("public_site", __name__)
REASONS = {
    "target_reached": "已收集三份有效回答", "attempts_exhausted": "已达到本轮尝试次数上限",
    "upstream_error": "上游检测失败", "task_timeout": "检测超时", "queue_timeout": "排队超时",
    "user_cancelled": "检测已取消", "worker_lost": "检测中断", "worker_shutdown": "检测中断", "worker_error": "检测中断",
}


def result_view(result):
    if not result:
        return None
    return {**{key: result.get(key) for key in ("prediction_name", "family_prediction_name", "probability", "sample_warning", "used_outputs")},
            "results": [{key: item.get(key) for key in ("display_name", "probability")}
                        for item in result.get("results", [])[:5]]}


def task_view(task, public_id=None):
    start = task.get("started_at")
    view = {"id": public_id, "status": task["status"], "model": task.get("requested_model"),
            "reasoning_effort": task.get("reasoning_effort"), "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"), "note": REASONS.get(task.get("reason"), ""),
            "progress": {key: task["progress"].get(key) for key in ("target", "valid", "attempted", "max_attempts")},
            "elapsed_seconds": round((task.get("finished_at") or time.time()) - start, 2) if start else 0,
            "result": result_view(task.get("result"))}
    if public_id is not None:
        view["url"] = task.get("base_url")
    return view


def monitor_view(record, detailed=False):
    history = [task_view(task) for task in (record["history"] if detailed else record["history"][:1])]
    view = {"id": record["public_id"], "name": record.get("public_name") or record["model"],
            "model": record["model"], "reasoning_effort": record["reasoning_effort"],
            "enabled": record["enabled"], "interval_seconds": record["interval_seconds"],
            "latest": history[0] if history else None}
    if detailed:
        view["history"] = history
    return view


class PublicStore:
    def __init__(self, tasks):
        self.tasks = tasks
        self.redis = tasks.redis

    def find(self, public_id, include_hidden=False):
        task_id = self.redis.get(self.tasks.public_key(public_id))
        task = self.tasks.get(task_id) if task_id else None
        if not task or task.get("public_id") != public_id or (task.get("feed_hidden") and not include_hidden):
            return None
        return task

    def feed(self, page=1, include_hidden=False):
        index = PUBLIC_INDEX if include_hidden else PUBLIC_VISIBLE_INDEX
        while True:
            total = self.redis.zcard(index)
            pages = max(1, (total + 19) // 20)
            page = min(page, pages)
            rows, stale = [], []
            for public_id in self.redis.zrevrange(index, (page - 1) * 20, page * 20 - 1):
                task = self.find(public_id, include_hidden=True)
                if not task:
                    stale.append(public_id)
                elif include_hidden or not task.get("feed_hidden"):
                    view = task_view(task, public_id)
                    if include_hidden:
                        view["hidden"] = bool(task.get("feed_hidden"))
                    rows.append(view)
                else:
                    self.redis.zrem(PUBLIC_VISIBLE_INDEX, public_id)
            if not stale:
                return {"items": rows, "page": page, "pages": pages, "total": total}
            with self.redis.pipeline() as pipe:
                pipe.zrem(PUBLIC_INDEX, *stale)
                pipe.zrem(PUBLIC_VISIBLE_INDEX, *stale)
                pipe.zrem(PUBLIC_EXPIRY, *stale)
                pipe.execute()

    def hide(self, public_id, hidden):
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    lookup = self.tasks.public_key(public_id)
                    pipe.watch(lookup)
                    task_id = pipe.get(lookup)
                    if not task_id:
                        return False
                    key = self.tasks.key(task_id)
                    pipe.watch(key)
                    raw = pipe.get(key)
                    ttl = pipe.ttl(key)
                    if not raw or ttl < 1:
                        return False
                    record = json.loads(raw)
                    record["feed_hidden"] = hidden
                    pipe.multi()
                    pipe.set(key, json.dumps(record), ex=ttl)
                    if hidden:
                        pipe.zrem(PUBLIC_VISIBLE_INDEX, public_id)
                    else:
                        pipe.zadd(PUBLIC_VISIBLE_INDEX, {public_id: record["created_at"]})
                    pipe.execute()
                    return True
                except WatchError:
                    continue


@api.before_request
def prepare_store():
    try:
        MonitorStore(store()).migrate_visibility()
    except (RedisError, ValueError):
        return jsonify(error="服务暂不可用，请稍后重试"), 503


@api.errorhandler(RedisError)
def unavailable(error):
    return jsonify(error="服务暂不可用，请稍后重试"), 503


@api.get("/public/api/home")
def home():
    backend = MonitorStore(store())
    records = []
    for key in backend.redis.scan_iter(match="modeltrace:monitor:*"):
        raw = backend.redis.get(key)
        if raw:
            record = json.loads(raw)
            if record.get("published"):
                records.append(record)
    records.sort(key=lambda row: (row.get("public_order", 0), row["created_at"], row["id"]))
    settings = SiteSettings(backend.redis).get()
    return jsonify(settings={key: settings[key] for key in ("title", "description", "announcement", "public_enabled")},
                   monitors=[monitor_view(record) for record in records])


@api.get("/public/api/monitors/<public_id>")
def monitor(public_id):
    if not valid_id(public_id):
        return jsonify(error="公开监测不存在"), 404
    backend = MonitorStore(store())
    for key in backend.redis.scan_iter(match="modeltrace:monitor:*"):
        raw = backend.redis.get(key)
        if raw:
            record = json.loads(raw)
            if record.get("published") and record.get("public_id") == public_id:
                return jsonify(monitor_view(record, detailed=True))
    return jsonify(error="公开监测不存在"), 404


@api.route("/public/api/detections", methods=["GET", "POST"])
def detections():
    backend = store()
    if request.method == "GET":
        return feed_response()
    payload = request.get_json(silent=True)
    try:
        if not isinstance(payload, dict) or set(payload) != {"url", "key", "model", "reasoning_effort"} or payload["reasoning_effort"] is None:
            raise ValueError("请填写 URL、Key、模型和思考强度")
        config = validate_submission({"base_url": payload["url"], "api_key": payload["key"],
                                      "model": payload["model"], "reasoning_effort": payload["reasoning_effort"]})
        backend.reap()
        public_id = uuid.uuid4().hex
        task = backend.submit({**config, "public_access": True, "public_id": public_id}, client_ip=request.remote_addr)
        return jsonify(task_view(task, public_id)), 202, {"Location": f"/public/api/detections/{public_id}", "Retry-After": "3"}
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except QueueFull as error:
        return jsonify(error=str(error)), 429, {"Retry-After": "60"}
    except PublicDisabled as error:
        return jsonify(error=str(error)), 403


def feed_response(include_hidden=False):
    try:
        page = int(request.args.get("page", "1"))
        if page < 1:
            raise ValueError
    except ValueError:
        return jsonify(error="页码无效"), 400
    store().reap()
    return jsonify(PublicStore(store()).feed(page, include_hidden))


@api.get("/public/api/detections/<public_id>")
def detection(public_id):
    if not valid_id(public_id):
        return jsonify(error="公开记录不存在或已过期"), 404
    store().reap()
    task = PublicStore(store()).find(public_id)
    if not task:
        return jsonify(error="公开记录不存在或已过期"), 404
    return jsonify(task_view(task, public_id))


@api.route("/admin/api/site", methods=["GET", "PATCH"])
def settings():
    backend = SiteSettings(store().redis)
    try:
        return jsonify(backend.get() if request.method == "GET" else backend.update(request.get_json(silent=True)))
    except ValueError as error:
        return jsonify(error=str(error)), 400


@api.get("/admin/api/detections")
def admin_feed():
    return feed_response(include_hidden=True)


@api.patch("/admin/api/detections/<public_id>")
def moderate(public_id):
    payload = request.get_json(silent=True)
    if not valid_id(public_id) or not isinstance(payload, dict) or set(payload) != {"hidden"} or not isinstance(payload["hidden"], bool):
        return jsonify(error="请提供有效的记录和隐藏状态"), 400
    if not PublicStore(store()).hide(public_id, payload["hidden"]):
        return jsonify(error="公开记录不存在或已过期"), 404
    return jsonify(ok=True)
