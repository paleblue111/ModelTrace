"""Durable Redis queue; atomic claims, terminal transitions and expiring leases."""
from __future__ import annotations

import json
import os
import time
import uuid

from cryptography.fernet import Fernet
from redis import Redis, WatchError

TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
QUEUE = "modeltrace:queue"
LEASES = "modeltrace:leases"
LEASE_SECONDS = 30


class QueueFull(RuntimeError):
    pass


def positive_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} 必须大于零")
    return value


class DetectionStore:
    def __init__(self, client=None):
        self.redis = client if client is not None else Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
        if not key:
            raise ValueError("需要配置 CREDENTIAL_ENCRYPTION_KEY")
        self.cipher = Fernet(key.encode())
        self.retention = positive_env("RESULT_TTL_SECONDS", 86400)
        self.queue_timeout = positive_env("QUEUE_TIMEOUT_SECONDS", 3600)
        self.task_timeout = positive_env("TASK_TIMEOUT_SECONDS", 1800)
        self.request_timeout = positive_env("REQUEST_TIMEOUT_SECONDS", 240)
        self.queue_limit = positive_env("MAX_QUEUED_TASKS", 100)

    @staticmethod
    def key(task_id):
        return f"modeltrace:task:{task_id}"

    @staticmethod
    def credential_key(task_id):
        return f"modeltrace:credential:{task_id}"

    def get(self, task_id):
        value = self.redis.get(self.key(task_id))
        return json.loads(value) if value else None

    def submit(self, config):
        now = time.time()
        task_id = uuid.uuid4().hex
        record = {
            "id": task_id, "status": "queued", "phase": "queued",
            "created_at": now, "started_at": None, "finished_at": None,
            "updated_at": now, "queue_deadline": now + self.queue_timeout,
            "deadline": None, "lease_until": now + self.queue_timeout,
            "base_url": config["base_url"], "requested_model": config["model"],
            "temperature": config.get("temperature"),
            "progress": {"target": 3, "valid": 0, "attempted": 0, "max_attempts": 6},
            "errors": [], "result": None, "samples": [], "reason": None,
            "preliminary": False, "insufficient_samples": True,
            "bank_sha256": None,
        }
        encrypted = self.cipher.encrypt(config["api_key"].encode()).decode()
        ttl = self.retention + self.queue_timeout + self.task_timeout
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(QUEUE)
                    if pipe.llen(QUEUE) >= self.queue_limit:
                        raise QueueFull("检测队列已满，请稍后重试")
                    pipe.multi()
                    pipe.set(self.key(task_id), json.dumps(record), ex=ttl)
                    pipe.set(self.credential_key(task_id), encrypted, ex=self.queue_timeout + self.task_timeout)
                    pipe.rpush(QUEUE, task_id)
                    pipe.zadd(LEASES, {task_id: record["lease_until"]})
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def update(self, task_id, mutate):
        """mutate returns False to skip; terminal records cannot be overwritten."""
        key = self.key(task_id)
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        return None
                    record = json.loads(raw)
                    if record["status"] in TERMINAL or mutate(record) is False:
                        return record
                    now = time.time()
                    record["updated_at"] = now
                    done = record["status"] in TERMINAL
                    if done:
                        record["finished_at"] = now
                        record["phase"] = "finished"
                        record["insufficient_samples"] = record["progress"]["valid"] < 3
                        record["preliminary"] = bool(record["result"]) and record["insufficient_samples"]
                    pipe.multi()
                    pipe.set(key, json.dumps(record), ex=self.retention if done else self.retention + self.task_timeout + self.queue_timeout)
                    if done:
                        pipe.delete(self.credential_key(task_id))
                        pipe.lrem(QUEUE, 0, task_id)
                        pipe.zrem(LEASES, task_id)
                    else:
                        pipe.zadd(LEASES, {task_id: record["lease_until"]})
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def finish(self, task_id, status, reason):
        def mutate(record):
            record.update(status=status, reason=reason)
        return self.update(task_id, mutate)

    def claim(self):
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(QUEUE)
                    task_id = pipe.lindex(QUEUE, 0)
                    if not task_id:
                        return None
                    key = self.key(task_id)
                    pipe.watch(key)
                    raw = pipe.get(key)
                    record = json.loads(raw) if raw else None
                    now = time.time()
                    if record and record["status"] == "queued" and record["queue_deadline"] > now:
                        record.update(status="running", phase="sampling", started_at=now,
                                      updated_at=now, deadline=now + self.task_timeout,
                                      lease_until=now + LEASE_SECONDS)
                    else:
                        record = None
                    pipe.multi()
                    pipe.lpop(QUEUE)
                    if record:
                        pipe.set(key, json.dumps(record), ex=self.retention + self.task_timeout)
                        pipe.zadd(LEASES, {task_id: record["lease_until"]})
                    pipe.execute()
                    if record:
                        return record
                except WatchError:
                    continue

    def heartbeat(self, task_id):
        def mutate(record):
            if record["lease_until"] <= time.time():
                record.update(status="interrupted", reason="worker_lost")
                return
            record["lease_until"] = time.time() + LEASE_SECONDS
        return self.update(task_id, mutate)

    def credential(self, task_id):
        token = self.redis.get(self.credential_key(task_id))
        if not token:
            raise ValueError("任务凭据已过期")
        return self.cipher.decrypt(token.encode()).decode()

    def reap(self):
        now = time.time()
        for task_id in self.redis.zrangebyscore(LEASES, "-inf", now):
            def mutate(record):
                if record["lease_until"] > now:
                    return False
                if record["status"] == "queued":
                    record.update(status="failed", reason="queue_timeout")
                else:
                    record.update(status="interrupted", reason="worker_lost")
            if self.update(task_id, mutate) is None:
                self.redis.zrem(LEASES, task_id)
                self.redis.lrem(QUEUE, 0, task_id)
                self.redis.delete(self.credential_key(task_id))


def public_record(record):
    hidden = {"queue_deadline", "deadline", "lease_until"}
    result = {key: value for key, value in record.items() if key not in hidden}
    start = record["started_at"]
    result["elapsed_seconds"] = round((record["finished_at"] or time.time()) - start, 2) if start else 0
    result["status_url"] = f"/api/detections/{record['id']}"
    return result
