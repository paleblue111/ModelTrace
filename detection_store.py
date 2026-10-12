"""Durable Redis queue; atomic claims, terminal transitions and expiring leases."""
from __future__ import annotations

import json
import hashlib
import os
import time
import uuid

from cryptography.fernet import Fernet
from redis import Redis, WatchError
from site_settings import SETTINGS, SiteSettings

TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
QUEUE = "modeltrace:queue"
PUBLIC_QUEUE = "modeltrace:public:queue"
PUBLIC_RUNNING = "modeltrace:public:running"
PUBLIC_INDEX = "modeltrace:public:results"
PUBLIC_VISIBLE_INDEX = "modeltrace:public:visible"
PUBLIC_EXPIRY = "modeltrace:public:expiry"
LEASES = "modeltrace:leases"
LEASE_SECONDS = 30


class QueueFull(RuntimeError):
    pass


class PublicDisabled(RuntimeError):
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

    def prepare(self, config):
        now = time.time()
        task_id = uuid.uuid4().hex
        record = {
            "id": task_id, "status": "queued", "phase": "queued",
            "created_at": now, "started_at": None, "finished_at": None,
            "updated_at": now, "queue_deadline": now + self.queue_timeout,
            "deadline": None, "lease_until": now + self.queue_timeout,
            "base_url": config["base_url"], "requested_model": config["model"],
            "temperature": config.get("temperature"),
            "reasoning_effort": config.get("reasoning_effort"),
            "public_access": config.get("public_access", False),
            "task_class": "public" if config.get("public_access") else "admin",
            "monitor_id": config.get("monitor_id"),
            "public_id": config.get("public_id"), "feed_hidden": False,
            "progress": {"target": 3, "valid": 0, "attempted": 0, "max_attempts": 6},
            "errors": [], "result": None, "samples": [], "reason": None,
            "preliminary": False, "insufficient_samples": True,
            "bank_sha256": None,
        }
        encrypted = self.cipher.encrypt(config["api_key"].encode()).decode()
        return record, encrypted

    def enqueue(self, pipe, record, encrypted):
        task_id = record["id"]
        ttl = self.retention + self.queue_timeout + self.task_timeout
        pipe.set(self.key(task_id), json.dumps(record), ex=ttl)
        pipe.set(self.credential_key(task_id), encrypted, ex=self.queue_timeout + self.task_timeout)
        pipe.rpush(PUBLIC_QUEUE if record["task_class"] == "public" else QUEUE, task_id)
        pipe.zadd(LEASES, {task_id: record["lease_until"]})
        if record.get("public_id"):
            pipe.set(self.public_key(record["public_id"]), task_id, ex=ttl)
            pipe.zadd(PUBLIC_INDEX, {record["public_id"]: record["created_at"]})
            pipe.zadd(PUBLIC_VISIBLE_INDEX, {record["public_id"]: record["created_at"]})
            pipe.zadd(PUBLIC_EXPIRY, {record["public_id"]: time.time() + ttl})

    @staticmethod
    def public_key(public_id):
        return f"modeltrace:public:result:{public_id}"

    def submit(self, config, client_ip=None):
        record, encrypted = self.prepare(config)
        anonymous = record["task_class"] == "public"
        client_hash = hashlib.sha256((os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "") + (client_ip or "unknown")).encode()).hexdigest()
        rate_key = f"modeltrace:public:rate:{client_hash}"
        active_key = f"modeltrace:public:pending:{client_hash}"
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(QUEUE, PUBLIC_QUEUE, SETTINGS)
                    if pipe.llen(QUEUE) + pipe.llen(PUBLIC_QUEUE) >= self.queue_limit:
                        raise QueueFull("检测队列已满，请稍后重试")
                    now = time.time()
                    finished = []
                    if anonymous:
                        settings = SiteSettings(self.redis).get(pipe)
                        if not settings["public_enabled"]:
                            raise PublicDisabled("管理员暂未开放单次检测")
                        if pipe.llen(PUBLIC_QUEUE) >= settings["public_queue_limit"]:
                            raise QueueFull("公共检测队列已满，请稍后重试")
                        pipe.watch(rate_key, active_key)
                        if pipe.zcount(rate_key, now - 3600, "+inf") >= settings["public_hourly_limit"]:
                            raise QueueFull("本小时检测次数已达上限，请稍后再试")
                        active_ids = pipe.zrange(active_key, 0, -1)
                        active_count = 0
                        for active_id in active_ids:
                            pipe.watch(self.key(active_id))
                            raw = pipe.get(self.key(active_id))
                            if raw and json.loads(raw)["status"] not in TERMINAL:
                                active_count += 1
                            else:
                                finished.append(active_id)
                        if active_count >= settings["public_pending_limit"]:
                            raise QueueFull("已有未完成检测，请等待结束后再提交")
                        record["client_hash"] = client_hash
                    pipe.multi()
                    self.enqueue(pipe, record, encrypted)
                    if anonymous:
                        pipe.zremrangebyscore(rate_key, "-inf", now - 3600)
                        pipe.zadd(rate_key, {record["id"]: now})
                        pipe.expire(rate_key, 3600)
                        if finished:
                            pipe.zrem(active_key, *finished)
                        pipe.zadd(active_key, {record["id"]: now})
                        pipe.expire(active_key, self.queue_timeout + self.task_timeout)
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
                        pipe.lrem(PUBLIC_QUEUE, 0, task_id)
                        pipe.zrem(PUBLIC_RUNNING, task_id)
                        pipe.zrem(LEASES, task_id)
                        if record.get("client_hash"):
                            pipe.zrem(f"modeltrace:public:pending:{record['client_hash']}", task_id)
                        if record.get("public_id"):
                            pipe.expire(self.public_key(record["public_id"]), self.retention)
                            pipe.zadd(PUBLIC_EXPIRY, {record["public_id"]: now + self.retention})
                    else:
                        pipe.zadd(LEASES, {task_id: record["lease_until"]})
                        if record.get("task_class") == "public" and record["status"] == "running":
                            pipe.zadd(PUBLIC_RUNNING, {task_id: record["lease_until"]})
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
                    pipe.watch(QUEUE, PUBLIC_QUEUE, PUBLIC_RUNNING, SETTINGS)
                    queue = QUEUE
                    task_id = pipe.lindex(QUEUE, 0)
                    if not task_id:
                        settings = SiteSettings(self.redis).get(pipe)
                        if pipe.zcard(PUBLIC_RUNNING) >= settings["public_concurrency"]:
                            return None
                        queue = PUBLIC_QUEUE
                        task_id = pipe.lindex(queue, 0)
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
                    pipe.lpop(queue)
                    if record:
                        pipe.set(key, json.dumps(record), ex=self.retention + self.task_timeout)
                        pipe.zadd(LEASES, {task_id: record["lease_until"]})
                        if queue == PUBLIC_QUEUE:
                            pipe.zadd(PUBLIC_RUNNING, {task_id: record["lease_until"]})
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
                self.redis.lrem(PUBLIC_QUEUE, 0, task_id)
                self.redis.zrem(PUBLIC_RUNNING, task_id)
                self.redis.delete(self.credential_key(task_id))
        self.cleanup_public_results()

    def cleanup_public_results(self):
        # Indexes have no Redis TTL; expire their members with the task retention.
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(PUBLIC_EXPIRY)
                    expired = pipe.zrangebyscore(PUBLIC_EXPIRY, "-inf", time.time(), start=0, num=1000)
                    if not expired:
                        return
                    pipe.multi()
                    pipe.zrem(PUBLIC_INDEX, *expired)
                    pipe.zrem(PUBLIC_VISIBLE_INDEX, *expired)
                    pipe.zrem(PUBLIC_EXPIRY, *expired)
                    pipe.delete(*(self.public_key(public_id) for public_id in expired))
                    pipe.execute()
                    if len(expired) < 1000:
                        return
                except WatchError:
                    continue


def public_record(record):
    hidden = {"queue_deadline", "deadline", "lease_until", "public_access", "task_class", "monitor_id", "public_id", "feed_hidden", "client_hash"}
    result = {key: value for key, value in record.items() if key not in hidden}
    start = record["started_at"]
    result["elapsed_seconds"] = round((record["finished_at"] or time.time()) - start, 2) if start else 0
    result["status_url"] = f"/api/detections/{record['id']}"
    return result
