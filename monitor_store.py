"""Server-owned recurring detections, with atomic scheduling and encrypted credentials."""
from __future__ import annotations

import json
import time
import uuid

from redis import WatchError

from detection_store import QUEUE, TERMINAL, public_record

DUE = "modeltrace:monitors:due"


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

    def prepare(self, config):
        now = time.time()
        record = {"id": uuid.uuid4().hex, "created_at": now, "updated_at": now,
                  "name": config.get("name", ""),
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
        if monitor_id and not self.get(monitor_id):
            raise MonitorNotFound("原监测配置不存在")
        record = {"id": uuid.uuid4().hex, "created_at": time.time(),
                  "monitor_ids": [monitor_id] if monitor_id else []}
        self.redis.set(self.dashboard_key(record["id"]), json.dumps(record))
        return record

    def get_dashboard(self, dashboard_id):
        raw = self.redis.get(self.dashboard_key(dashboard_id))
        return json.loads(raw) if raw else None

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
                  "run_requested", "active_task_id", "next_run_at", "updated_at", "last_error", "total_runs")
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
                    if changes.get("run_requested") and (record["active_task_id"] or record["run_requested"]):
                        raise MonitorBusy("已有检测任务，完成后再试")
                    now = time.time()
                    for field in ("name", "url", "model", "reasoning_effort", "interval_seconds", "enabled", "run_requested"):
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

    def tick(self, monitor_id):
        """Queue creation and active_task_id commit together, including across schedulers."""
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.key(monitor_id), QUEUE)
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
                        if pipe.llen(QUEUE) >= self.tasks.queue_limit:
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
                                    "model": record["model"], "reasoning_effort": record["reasoning_effort"], "public_access": True})
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
