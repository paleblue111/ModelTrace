"""Server-owned recurring detections, with atomic scheduling and encrypted credentials."""
from __future__ import annotations

import json
import time
import uuid

from redis import WatchError

from detection_store import LEASES, PUBLIC_QUEUE, PUBLIC_RUNNING, QUEUE, TERMINAL, public_record

DUE = "modeltrace:monitors:due"
DEFAULT = "modeltrace:monitors:default"
DEFAULT_DASHBOARD = "modeltrace:dashboards:default"
VISIBILITY_VERSION = "modeltrace:migrations:public-site-v1"


class MonitorBusy(ValueError):
    pass


class MonitorNotFound(ValueError):
    pass


class MonitorStore:
    def __init__(self, detections):
        self.tasks = detections
        self.redis = detections.redis

    @staticmethod
    def key(monitor_id):
        return f"modeltrace:monitor:{monitor_id}"

    def get(self, monitor_id):
        raw = self.redis.get(self.key(monitor_id))
        return json.loads(raw) if raw else None

    def migrate_visibility(self):
        if self.redis.get(VISIBILITY_VERSION):
            return
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(VISIBILITY_VERSION, QUEUE, PUBLIC_QUEUE, PUBLIC_RUNNING)
                    if pipe.get(VISIBILITY_VERSION):
                        return
                    records = []
                    monitor_tasks = set()
                    for key in self.redis.scan_iter(match="modeltrace:monitor:*"):
                        pipe.watch(key)
                        raw = pipe.get(key)
                        if not raw:
                            continue
                        record = json.loads(raw)
                        record.setdefault("published", False)
                        record.setdefault("public_name", "")
                        record.setdefault("public_order", 0)
                        record.setdefault("public_id", uuid.uuid4().hex)
                        records.append((key, record))
                        monitor_tasks.update(item["id"] for item in record["history"])
                        if record["active_task_id"]:
                            monitor_tasks.add(record["active_task_id"])
                    tasks = []
                    for key in self.redis.scan_iter(match="modeltrace:task:*"):
                        pipe.watch(key)
                        raw = pipe.get(key)
                        if not raw:
                            continue
                        task = json.loads(raw)
                        known_origin = "task_class" in task
                        task.setdefault("task_class", "public" if task.get("public_access") else "admin")
                        if task["id"] in monitor_tasks or task.get("monitor_id"):
                            task["public_access"] = False
                            task["task_class"] = "admin"
                        elif not known_origin:
                            # Old unreferenced records cannot prove whether they came from a monitor.
                            # Keep executing them, but require administrator access to their result.
                            task["public_access"] = False
                        tasks.append((key, task, max(1, pipe.ttl(key))))
                    old_queue = pipe.lrange(QUEUE, 0, -1)
                    public_ids = {item[1]["id"] for item in tasks if item[1]["task_class"] == "public"}
                    pipe.multi()
                    for key, record in records:
                        pipe.set(key, json.dumps(record))
                    for key, task, ttl in tasks:
                        pipe.set(key, json.dumps(task), ex=ttl)
                        if task["task_class"] == "public" and task["status"] == "running":
                            pipe.zadd(PUBLIC_RUNNING, {task["id"]: task["lease_until"]})
                    for task_id in old_queue:
                        if task_id in public_ids:
                            pipe.lrem(QUEUE, 0, task_id)
                            pipe.rpush(PUBLIC_QUEUE, task_id)
                    pipe.set(VISIBILITY_VERSION, "1")
                    pipe.execute()
                    return
                except WatchError:
                    continue

    def default(self, config=None):
        """Resolve one shared monitor, adopting the oldest existing configuration."""
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(DEFAULT)
                    monitor_id = pipe.get(DEFAULT)
                    record = self.get(monitor_id) if monitor_id else None
                    if record:
                        return record
                    records = []
                    for key in self.redis.scan_iter(match="modeltrace:monitor:*"):
                        pipe.watch(key)
                        raw = pipe.get(key)
                        if raw:
                            records.append(json.loads(raw))
                    record = min(records, key=lambda item: (item["created_at"], item["id"])) if records else None
                    if record is None and config is None:
                        return None
                    new_record = record is None
                    if new_record:
                        record = self.prepare(config)
                    pipe.multi()
                    if new_record:
                        pipe.set(self.key(record["id"]), json.dumps(record))
                    pipe.set(DEFAULT, record["id"])
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def prepare(self, config):
        now = time.time()
        record = {"id": uuid.uuid4().hex, "created_at": now, "updated_at": now,
                  "name": config.get("name", ""),
                  "published": config.get("published", False), "public_name": config.get("public_name", ""),
                  "public_order": config.get("public_order", 0), "public_id": uuid.uuid4().hex,
                  "url": config["base_url"], "model": config["model"],
                  "reasoning_effort": config["reasoning_effort"], "interval_seconds": config["interval_seconds"],
                  "encrypted_key": self.tasks.cipher.encrypt(config["api_key"].encode()).decode(),
                  "enabled": False, "run_requested": False, "next_run_at": None,
                  "active_task_id": None, "history": [], "total_runs": 0, "last_error": None}
        return record

    def create(self, config):
        record = self.prepare(config)
        self.redis.set(self.key(record["id"]), json.dumps(record))
        return record

    @staticmethod
    def dashboard_key(dashboard_id):
        return f"modeltrace:dashboard:{dashboard_id}"

    def create_dashboard(self, monitor_id=None):
        if monitor_id:
            return self.adopt_monitor(monitor_id)
        record = {"id": uuid.uuid4().hex, "created_at": time.time(),
                  "monitor_ids": []}
        self.redis.set(self.dashboard_key(record["id"]), json.dumps(record))
        return record

    def adopt_monitor(self, monitor_id):
        """Resolve legacy bookmarks into the shared panel without duplicating panels."""
        if not self.get(monitor_id):
            raise MonitorNotFound("原监测配置不存在")
        dashboard = self.default_dashboard()
        key = self.dashboard_key(dashboard["id"])
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key, self.key(monitor_id))
                    if not pipe.get(self.key(monitor_id)):
                        raise MonitorNotFound("原监测配置不存在")
                    dashboard = json.loads(pipe.get(key))
                    if monitor_id in dashboard["monitor_ids"]:
                        return dashboard
                    if len(dashboard["monitor_ids"]) >= 100:
                        raise ValueError("每个面板最多保存 100 个监测项")
                    dashboard["monitor_ids"].append(monitor_id)
                    pipe.multi()
                    pipe.set(key, json.dumps(dashboard))
                    pipe.execute()
                    return dashboard
                except WatchError:
                    continue

    def get_dashboard(self, dashboard_id):
        raw = self.redis.get(self.dashboard_key(dashboard_id))
        return json.loads(raw) if raw else None

    def default_dashboard(self):
        """Use one server-owned panel across browsers, adopting existing data once."""
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(DEFAULT_DASHBOARD)
                    dashboard_id = pipe.get(DEFAULT_DASHBOARD)
                    record = self.get_dashboard(dashboard_id) if dashboard_id else None
                    if record:
                        return record
                    dashboards = []
                    for key in self.redis.scan_iter(match="modeltrace:dashboard:*"):
                        raw = self.redis.get(key)
                        if raw:
                            dashboards.append(json.loads(raw))
                    record = min(dashboards, key=lambda item: (item["created_at"], item["id"])) if dashboards else None
                    if record:
                        # Preserve membership if another browser adds a monitor during adoption.
                        key = self.dashboard_key(record["id"])
                        pipe.watch(key)
                        raw = pipe.get(key)
                        if not raw:
                            continue
                        record = json.loads(raw)
                    else:
                        monitors = []
                        for key in self.redis.scan_iter(match="modeltrace:monitor:*"):
                            pipe.watch(key)
                            raw = pipe.get(key)
                            if raw:
                                monitors.append(json.loads(raw))
                        monitors.sort(key=lambda item: (item["created_at"], item["id"]))
                        record = {"id": uuid.uuid4().hex, "created_at": time.time(),
                                  "monitor_ids": [item["id"] for item in monitors[:100]]}
                    pipe.multi()
                    pipe.set(self.dashboard_key(record["id"]), json.dumps(record))
                    pipe.set(DEFAULT_DASHBOARD, record["id"])
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def add_to_dashboard(self, dashboard_id, config):
        # A new monitor and its membership must be committed together.
        record = self.prepare(config)
        key = self.dashboard_key(dashboard_id)
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        raise MonitorNotFound("监测面板不存在")
                    dashboard = json.loads(raw)
                    if len(dashboard["monitor_ids"]) >= 100:
                        raise ValueError("每个面板最多保存 100 个监测项")
                    dashboard["monitor_ids"].append(record["id"])
                    pipe.multi()
                    pipe.set(key, json.dumps(dashboard))
                    pipe.set(self.key(record["id"]), json.dumps(record))
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def public_dashboard(self, record):
        ids = record["monitor_ids"]
        raw_records = self.redis.mget([self.key(item) for item in ids]) if ids else []
        monitors = [self.summary(json.loads(raw)) for raw in raw_records if raw]
        heartbeat = self.redis.get("modeltrace:scheduler:heartbeat")
        return {"id": record["id"], "monitors": monitors,
                "scheduler_online": bool(heartbeat and time.time() - float(heartbeat) < 15)}

    @staticmethod
    def summary(record):
        # Polling a list must not transfer every monitor's samples and history.
        fields = ("id", "name", "url", "model", "reasoning_effort", "interval_seconds", "enabled",
                  "run_requested", "active_task_id", "next_run_at", "updated_at", "last_error", "total_runs", "published", "public_name", "public_order")
        data = {field: record.get(field) for field in fields}
        latest = record["history"][0] if record["history"] else None
        result = (latest.get("result") or {}) if latest else {}
        data["latest"] = {"status": latest["status"], "finished_at": latest["finished_at"],
                          "prediction_name": result.get("prediction_name"),
                          "probability": result.get("probability"),
                          "valid_samples": latest["progress"]["valid"]} if latest else None
        return data

    def save(self, pipe, record):
        pipe.set(self.key(record["id"]), json.dumps(record))
        if record["active_task_id"]:
            pipe.zadd(DUE, {record["id"]: time.time() + 2})
        elif record["enabled"] or record["run_requested"]:
            pipe.zadd(DUE, {record["id"]: record["next_run_at"] or time.time()})
        else:
            pipe.zrem(DUE, record["id"])

    def update(self, monitor_id, changes):
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.key(monitor_id))
                    raw = pipe.get(self.key(monitor_id))
                    if not raw:
                        return None
                    record = json.loads(raw)
                    # Check against the watched record, including after transaction retries.
                    # A stale editor must never send the current credential to another URL.
                    if "url" in changes and changes["url"] != record["url"] and not changes.get("api_key"):
                        raise ValueError("更换接口地址时请重新填写 API Key")
                    if changes.get("run_requested") and (record["active_task_id"] or record["run_requested"]):
                        raise MonitorBusy("已有检测任务，完成后再试")
                    now = time.time()
                    for field in ("name", "url", "model", "reasoning_effort", "interval_seconds", "enabled", "run_requested", "published", "public_name", "public_order"):
                        if field in changes:
                            record[field] = changes[field]
                    if changes.get("api_key"):
                        record["encrypted_key"] = self.tasks.cipher.encrypt(changes["api_key"].encode()).decode()
                    if changes.get("enabled") or changes.get("run_requested"):
                        record["next_run_at"] = now
                    elif changes.get("enabled") is False:
                        record["run_requested"] = False
                        record["next_run_at"] = None
                    elif "interval_seconds" in changes and record["enabled"] and not record["active_task_id"]:
                        record["next_run_at"] = now + record["interval_seconds"]
                    record["updated_at"] = now
                    record["last_error"] = None
                    pipe.multi()
                    self.save(pipe, record)
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def delete(self, monitor_id):
        """Remove configuration, membership and scheduling in one transaction."""
        key = self.key(monitor_id)
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key, DEFAULT, DEFAULT_DASHBOARD)
                    raw = pipe.get(key)
                    if not raw:
                        return False
                    record = json.loads(raw)
                    dashboards = []
                    for dashboard_key in self.redis.scan_iter(match="modeltrace:dashboard:*"):
                        pipe.watch(dashboard_key)
                        dashboard_raw = pipe.get(dashboard_key)
                        if dashboard_raw:
                            dashboard = json.loads(dashboard_raw)
                            if monitor_id in dashboard["monitor_ids"]:
                                dashboard["monitor_ids"] = [item for item in dashboard["monitor_ids"] if item != monitor_id]
                                dashboards.append((dashboard_key, dashboard))
                    is_default = pipe.get(DEFAULT) == monitor_id
                    task_id = record["active_task_id"]
                    task = None
                    if task_id:
                        task_key = self.tasks.key(task_id)
                        pipe.watch(task_key)
                        task_raw = pipe.get(task_key)
                        task = json.loads(task_raw) if task_raw else None
                        if task and task["status"] not in TERMINAL:
                            now = time.time()
                            task.update(status="cancelled", reason="user_cancelled", phase="finished",
                                        updated_at=now, finished_at=now,
                                        insufficient_samples=task["progress"]["valid"] < 3,
                                        preliminary=bool(task["result"]) and task["progress"]["valid"] < 3)
                    pipe.multi()
                    pipe.delete(key)
                    pipe.zrem(DUE, monitor_id)
                    if is_default:
                        pipe.delete(DEFAULT)
                    for dashboard_key, dashboard in dashboards:
                        pipe.set(dashboard_key, json.dumps(dashboard))
                    if task_id:
                        if task:
                            pipe.set(task_key, json.dumps(task), ex=self.tasks.retention)
                        pipe.delete(self.tasks.credential_key(task_id))
                        pipe.lrem(QUEUE, 0, task_id)
                        pipe.zrem(LEASES, task_id)
                    pipe.execute()
                    return True
                except WatchError:
                    continue

    def tick(self, monitor_id):
        """Queue creation and active_task_id commit together, including across schedulers."""
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.key(monitor_id), QUEUE, PUBLIC_QUEUE)
                    raw = pipe.get(self.key(monitor_id))
                    if not raw:
                        pipe.multi()
                        pipe.zrem(DUE, monitor_id)
                        pipe.execute()
                        return
                    record = json.loads(raw)
                    now = time.time()
                    new_task = None
                    if record["active_task_id"]:
                        task_key = self.tasks.key(record["active_task_id"])
                        pipe.watch(task_key)
                        task_raw = pipe.get(task_key)
                        task = json.loads(task_raw) if task_raw else None
                        if task is None or task["status"] in TERMINAL:
                            if task:
                                record["history"] = [public_record(task), *record["history"]][:20]
                            else:
                                record["last_error"] = "上一轮任务记录已过期或丢失"
                            record["total_runs"] += 1
                            record["active_task_id"] = None
                            # No catch-up burst after downtime: schedule relative to observation of completion.
                            record["next_run_at"] = now + record["interval_seconds"] if record["enabled"] else None
                    elif (record["enabled"] or record["run_requested"]) and (record["next_run_at"] or 0) <= now:
                        if pipe.llen(QUEUE) + pipe.llen(PUBLIC_QUEUE) >= self.tasks.queue_limit:
                            record["last_error"] = "检测队列已满，30 秒后重试"
                            record["next_run_at"] = now + 30
                        else:
                            try:
                                api_key = self.tasks.cipher.decrypt(record["encrypted_key"].encode()).decode()
                            except Exception:
                                record.update(enabled=False, run_requested=False, next_run_at=None,
                                              last_error="无法解密保存的 Key，请在设置中重新填写并保存")
                            else:
                                new_task = self.tasks.prepare({"base_url": record["url"], "api_key": api_key,
                                    "model": record["model"], "reasoning_effort": record["reasoning_effort"], "monitor_id": record["id"], "public_access": False})
                                record.update(active_task_id=new_task[0]["id"], next_run_at=None,
                                              run_requested=False, last_error=None)
                    record["updated_at"] = now
                    pipe.multi()
                    if new_task:
                        self.tasks.enqueue(pipe, *new_task)
                    self.save(pipe, record)
                    pipe.execute()
                    return
                except WatchError:
                    continue

    def public(self, record):
        data = {name: value for name, value in record.items() if name != "encrypted_key"}
        data["has_key"] = bool(record["encrypted_key"])
        active = self.tasks.get(record["active_task_id"]) if record["active_task_id"] else None
        data["active_task"] = public_record(active) if active else None
        heartbeat = self.redis.get("modeltrace:scheduler:heartbeat")
        data["scheduler_online"] = bool(heartbeat and time.time() - float(heartbeat) < 15)
        return data
