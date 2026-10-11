"""Run one task per worker; scale workers with Docker Compose."""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import signal
import time
from pathlib import Path

from redis.exceptions import RedisError

from detection_store import DetectionStore, TERMINAL
from fingerprint import analyze_global_outputs, generate_challenges, parse_numbers
from responses_client import UpstreamError, request_response

BANK_PATH = Path(__file__).with_name("data") / "unified_bank.json"
STOPPING = False


def response_process(sender, configuration):
    try:
        sender.send({"ok": True, **request_response(**configuration)})
    except UpstreamError as error:
        sender.send({"ok": False, "error": str(error), "retryable": error.retryable})
    except Exception:
        # Never log exception objects or upstream responses, which can contain keys.
        sender.send({"ok": False, "error": "上游调用失败", "retryable": False})
    finally:
        sender.close()


def stop_child(process):
    process.join(timeout=0.2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join()
    process.close()


def run_probe(backend, task_id, configuration, deadline):
    """A disposable process gives DNS, TLS and response reads a hard wall-clock limit."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=response_process, args=(sender, configuration))
    process.start()
    sender.close()
    started = time.monotonic()
    next_heartbeat = 0
    try:
        while True:
            if STOPPING:
                backend.finish(task_id, "interrupted", "worker_shutdown")
                return None
            record = backend.get(task_id)
            if not record or record["status"] in TERMINAL:
                return None
            now = time.time()
            if now >= deadline:
                return {"ok": False, "error": "任务达到总时限", "retryable": False, "task_timeout": True}
            if time.monotonic() - started >= backend.request_timeout:
                return {"ok": False, "error": "上游请求超时", "retryable": True}
            if now >= next_heartbeat:
                current = backend.heartbeat(task_id)
                if not current or current["status"] in TERMINAL:
                    return None
                next_heartbeat = now + 5
            if receiver.poll(0.2):
                try:
                    return receiver.recv()
                except EOFError:
                    return {"ok": False, "error": "上游请求进程中断", "retryable": True}
    finally:
        receiver.close()
        stop_child(process)


def record_attempt(backend, task_id, challenge, reply, bank, outputs):
    numbers = parse_numbers(reply["text"]) if reply["ok"] else []
    minimum = max(80, math.ceil(challenge["expected_count"] * 0.55))
    accepted = len(numbers) >= minimum
    # Normalize usage to numeric counters only. Never persist arbitrary provider metadata.
    usage = reply.get("usage")
    usage = {key: value for key, value in usage.items()
             if key in {"input_tokens", "output_tokens", "total_tokens"}
             and isinstance(value, int) and not isinstance(value, bool) and value >= 0} if isinstance(usage, dict) else {}
    if accepted:
        # Store parsed numeric content only; gateways may echo secrets in free-form text.
        outputs.append({"text": json.dumps(numbers), "expected_count": challenge["expected_count"]})
    result = analyze_global_outputs(outputs, bank) if outputs else None
    def mutate(record):
        record["progress"]["valid"] = len(outputs)
        record["samples"].append({"challenge_id": challenge["id"], "expected_count": challenge["expected_count"],
                                  "parsed_numbers": len(numbers), "accepted": accepted, "usage": usage})
        if not accepted:
            record["errors"].append({"attempt": record["progress"]["attempted"],
                                     "message": reply.get("error", f"有效数字不足：{len(numbers)}/{minimum}")})
        record["result"] = result
        record["preliminary"] = bool(result) and len(outputs) < 3
        record["insufficient_samples"] = len(outputs) < 3
    return backend.update(task_id, mutate)


def run_task(backend, record, probe=run_probe):
    task_id = record["id"]
    try:
        raw_bank = BANK_PATH.read_bytes()
        bank = json.loads(raw_bank)
        digest = hashlib.sha256(raw_bank).hexdigest()
        backend.update(task_id, lambda item: item.update(bank_sha256=digest))
        api_key = backend.credential(task_id)
        outputs = []
        reason = "attempts_exhausted"
        for challenge in generate_challenges(6):
            current = backend.get(task_id)
            if not current or current["status"] in TERMINAL:
                return
            if STOPPING:
                backend.finish(task_id, "interrupted", "worker_shutdown")
                return
            if time.time() >= record["deadline"]:
                reason = "task_timeout"
                break
            def attempting(item):
                item["progress"]["attempted"] += 1
                item["phase"] = "sampling"
            current = backend.update(task_id, attempting)
            if not current or current["status"] in TERMINAL:
                return
            reply = probe(backend, task_id, {
                "base_url": record["base_url"], "api_key": api_key, "model": record["requested_model"],
                "prompt": challenge["prompt"], "temperature": record["temperature"],
                "reasoning_effort": record.get("reasoning_effort"),
                "timeout": min(backend.request_timeout, max(0.1, record["deadline"] - time.time())),
            }, record["deadline"])
            if reply is None:
                return
            current = record_attempt(backend, task_id, challenge, reply, bank, outputs)
            if not current or current["status"] in TERMINAL:
                return
            if len(outputs) == 3:
                reason = "target_reached"
                break
            if not reply["ok"] and not reply["retryable"]:
                reason = "task_timeout" if reply.get("task_timeout") else "upstream_error"
                break
            # A challenge is one HTTP request. No hidden retries beyond the six-call budget.
            if not reply["ok"]:
                until = time.monotonic() + min(current["progress"]["attempted"], 5)
                while time.monotonic() < until and time.time() < record["deadline"] and not STOPPING:
                    if backend.get(task_id)["status"] in TERMINAL:
                        return
                    time.sleep(0.2)
        status = "completed" if len(outputs) == 3 else "partial" if outputs else "failed"
        backend.finish(task_id, status, reason)
    except RedisError:
        # Lease recovery marks the job interrupted; do not replay a possibly billed request.
        raise
    except Exception:
        backend.finish(task_id, "interrupted", "worker_error")


def request_shutdown(signum, frame):
    global STOPPING
    STOPPING = True


def main():
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    backend = DetectionStore()
    while not STOPPING:
        try:
            backend.reap()
            record = backend.claim()
            if record:
                run_task(backend, record)
            else:
                time.sleep(1)
        except RedisError:
            time.sleep(2)


if __name__ == "__main__":
    main()
