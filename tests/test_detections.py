import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import fakeredis
import pytest
from cryptography.fernet import Fernet

from detection_store import DetectionStore, LEASES, QUEUE
import detection_worker as worker
from responses_client import UpstreamError, request_response, responses_url


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MODELTRACE_API_TOKEN", "service-test-token")
    monkeypatch.setenv("ASYNC_ONLY", "true")
    monkeypatch.setenv("ALLOW_PRIVATE_UPSTREAM", "true")
    monkeypatch.delenv("UPSTREAM_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(worker, "STOPPING", False)
    return DetectionStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def client(backend):
    from app import app
    app.config.update(TESTING=True)
    app.extensions["detection_store"] = backend
    return app.test_client()


AUTH = {"Authorization": "Bearer service-test-token"}
CONFIG = {"base_url": "https://example.com/v1", "api_key": "secret-upstream-key", "model": "alias"}
NUMBERS = ",".join(str((index * 73) % 355 + 1) for index in range(330))


def success(*args):
    return {"ok": True, "text": NUMBERS, "usage": {"input_tokens": 12, "total_tokens": 100}}


def queued(backend):
    backend.submit(CONFIG)
    return backend.claim()


def test_submit_auth_validation_and_no_secret(client, backend):
    assert client.post("/api/detections", json=CONFIG).status_code == 401
    for data in ([], {}, {**CONFIG, "model": 2}, {**CONFIG, "temperature": True},
                 {**CONFIG, "temperature": float("nan")}, {**CONFIG, "api_key": "bad\nkey"},
                 {**CONFIG, "base_url": "https://user:pass@example.com"}, {**CONFIG, "extra": 1}):
        assert client.post("/api/detections", json=data, headers=AUTH).status_code == 400
    response = client.post("/api/detections", json=CONFIG, headers=AUTH)
    assert response.status_code == 202
    task = response.json
    assert task["status"] == "queued"
    assert response.headers["Location"] == task["status_url"]
    assert "secret-upstream-key" not in response.text
    assert "api_key" not in response.text
    assert backend.credential(task["id"]) == CONFIG["api_key"]
    assert CONFIG["api_key"] not in backend.redis.get(backend.credential_key(task["id"]))
    assert client.get(task["status_url"]).status_code == 401
    assert client.get("/api/detections/not-an-id", headers=AUTH).status_code == 404
    assert client.get(task["status_url"], headers=AUTH).headers["Cache-Control"] == "no-store"


def test_claim_once_and_parallel_submission_limit(backend):
    task = backend.submit(CONFIG)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: backend.claim(), range(4)))
    assert sum(item is not None for item in claims) == 1
    assert backend.get(task["id"])["status"] == "running"
    backend.queue_limit = 2
    from detection_store import QueueFull
    def submit(_):
        try:
            return backend.submit(CONFIG)
        except QueueFull:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(item is not None for item in pool.map(submit, range(4))) == 2


@pytest.mark.parametrize("valid_count", [0, 1, 2, 3])
def test_completed_partial_failed_and_candidate_results(backend, valid_count):
    task = queued(backend)
    calls = []
    def probe(*args):
        calls.append(args)
        return success() if len(calls) <= valid_count else {"ok": True, "text": "no numbers"}
    worker.run_task(backend, task, probe)
    result = backend.get(task["id"])
    assert result["status"] == ("completed" if valid_count == 3 else "partial" if valid_count else "failed")
    assert len(calls) == (3 if valid_count == 3 else 6)
    assert result["progress"]["valid"] == valid_count
    assert result["insufficient_samples"] == (valid_count < 3)
    assert result["preliminary"] == (0 < valid_count < 3)
    if valid_count:
        assert len(result["result"]["results"]) == 18
        assert result["result"]["used_outputs"] == valid_count
        assert result["result"]["calibration"]["queries"] == str(valid_count)
    assert len(result["bank_sha256"]) == 64
    assert backend.redis.get(backend.credential_key(task["id"])) is None
    assert backend.redis.ttl(backend.key(task["id"])) <= backend.retention


def test_permanent_upstream_failure_stops_and_retains_partial(backend):
    task = queued(backend)
    probe = Mock(side_effect=[success(), {"ok": False, "error": "上游 HTTP 401", "retryable": False}])
    worker.run_task(backend, task, probe)
    result = backend.get(task["id"])
    assert result["status"] == "partial"
    assert result["reason"] == "upstream_error"
    assert probe.call_count == 2


def test_cancellation_is_terminal_even_if_result_arrives(backend, client):
    task = queued(backend)
    def probe(*args):
        response = client.delete(f"/api/detections/{task['id']}", headers=AUTH)
        assert response.json["status"] == "cancelled"
        return success()
    worker.run_task(backend, task, probe)
    result = backend.get(task["id"])
    assert result["status"] == "cancelled"
    assert result["result"] is None
    assert result["progress"]["valid"] == 0
    assert backend.redis.get(backend.credential_key(task["id"])) is None
    assert client.delete(f"/api/detections/{task['id']}", headers=AUTH).json == client.get(f"/api/detections/{task['id']}", headers=AUTH).json


def test_cancel_queued_and_queue_backpressure(backend, client):
    backend.queue_limit = 1
    task = backend.submit(CONFIG)
    assert client.post("/api/detections", json=CONFIG, headers=AUTH).status_code == 429
    client.delete(f"/api/detections/{task['id']}", headers=AUTH)
    assert backend.claim() is None
    assert backend.redis.llen(QUEUE) == 0
    assert client.post("/api/detections", json=CONFIG, headers=AUTH).status_code == 202


def test_recovery_preserves_partial_and_never_requeues(backend):
    task = queued(backend)
    worker.record_attempt(backend, task["id"], {"id": "p", "expected_count": 300}, success(),
                          json.loads(worker.BANK_PATH.read_bytes()), [])
    backend.update(task["id"], lambda row: row.update(lease_until=time.time() - 1))
    backend.reap()
    recovered = backend.get(task["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["reason"] == "worker_lost"
    assert recovered["result"]["used_outputs"] == 1
    assert recovered["preliminary"] is True
    assert backend.claim() is None
    assert backend.redis.get(backend.credential_key(task["id"])) is None
    # A delayed heartbeat cannot resurrect an interrupted task.
    assert backend.heartbeat(task["id"])["status"] == "interrupted"


def test_queued_expiry_and_heartbeat_not_reaped(backend):
    task = backend.submit(CONFIG)
    backend.update(task["id"], lambda row: row.update(lease_until=time.time() - 1, queue_deadline=time.time() - 1))
    backend.reap()
    assert backend.get(task["id"])["reason"] == "queue_timeout"
    task = queued(backend)
    backend.heartbeat(task["id"])
    backend.reap()
    assert backend.get(task["id"])["status"] == "running"


def test_deadline_preserves_existing_sample(backend):
    task = queued(backend)
    def probe(*args):
        task["deadline"] = time.time() - 1
        return success()
    worker.run_task(backend, task, probe)
    result = backend.get(task["id"])
    assert result["status"] == "partial"
    assert result["reason"] == "task_timeout"


def test_server_mode_disables_sync_and_mutations(client):
    for path in ("/api/test/auto", "/api/test/probe", "/api/enroll/auto", "/api/banks"):
        assert client.post(path, json={}, headers=AUTH).status_code == 409
    page = client.get("/")
    assert page.status_code == 200
    assert b"service-token" in page.data


@pytest.mark.parametrize("base,expected", [
    ("https://example.com", "https://example.com/v1/responses"),
    ("https://example.com/v1/", "https://example.com/v1/responses"),
    ("https://example.com/proxy/v1/responses", "https://example.com/proxy/v1/responses"),
])
def test_url_normalization(base, expected):
    assert responses_url(base) == expected


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://example.com/v1/chat/completions",
                                      "https://example.com/v1/messages", "https://example.com/?key=secret",
                                      "https://example.com:abc", "https://example.com/a\nb"])
def test_invalid_url(url):
    with pytest.raises(ValueError):
        responses_url(url)


@pytest.fixture
def upstream():
    class Handler(BaseHTTPRequestHandler):
        requests = []
        payload = {"status": "completed", "output": [
            {"type": "reasoning", "summary": [{"text": "ignore"}]},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": NUMBERS}]}],
            "usage": {"input_tokens": 12}}
        code = 200
        delay = 0
        def do_POST(self):
            type(self).requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))), dict(self.headers)))
            time.sleep(self.delay)
            self.send_response(self.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(self.payload).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", Handler
    server.shutdown()
    server.server_close()
    thread.join()


def test_responses_transport_schema_and_no_conversation(backend, upstream):
    url, handler = upstream
    response = request_response(url, "test-key", "alias", "challenge", system_prompt="system")
    assert response["text"] == NUMBERS
    path, payload, headers = handler.requests[0]
    assert path == "/v1/responses"
    assert payload == {"model": "alias", "input": [{"role": "user", "content": "challenge"}],
                       "instructions": "system", "store": False, "stream": False}
    assert headers["Authorization"] == "Bearer test-key"
    assert "messages" not in payload and "tools" not in payload


@pytest.mark.parametrize("payload", [
    {"status": "incomplete", "output": []},
    {"status": "failed", "output": []},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": []},
    {"choices": [{"message": {"content": NUMBERS}}]},
])
def test_bad_responses_rejected(backend, upstream, payload):
    url, handler = upstream
    handler.payload = payload
    with pytest.raises(UpstreamError):
        request_response(url, "test-key", "alias", "challenge")


def test_http_errors_never_echo_key_and_no_redirect(backend, upstream):
    url, handler = upstream
    handler.payload = {"error": "echo test-key"}
    for code in (401, 429, 302):
        handler.code = code
        with pytest.raises(UpstreamError) as error:
            request_response(url, "test-key", "alias", "challenge")
        assert "test-key" not in str(error.value)
        assert error.value.retryable == (code == 429)
    assert len(handler.requests) == 3


def test_private_hosts_blocked_by_default(backend, upstream, monkeypatch):
    url, handler = upstream
    monkeypatch.delenv("ALLOW_PRIVATE_UPSTREAM")
    with pytest.raises(UpstreamError, match="禁止访问"):
        request_response(url, "key", "alias", "challenge")
    assert handler.requests == []


def test_process_worker_full_path_and_wall_timeout(backend, upstream):
    url, handler = upstream
    backend.submit({**CONFIG, "base_url": url})
    task = backend.claim()
    worker.run_task(backend, task)
    assert backend.get(task["id"])["status"] == "completed"
    assert len(handler.requests) == 3
    task = queued(backend)
    backend.request_timeout = 0.3
    handler.delay = 2
    started = time.monotonic()
    result = worker.run_probe(backend, task["id"], {
        "base_url": url, "api_key": "key", "model": "alias", "prompt": "challenge", "timeout": 5,
    }, time.time() + 10)
    assert result["error"] == "上游请求超时"
    assert time.monotonic() - started < 2


def test_missing_store_config_returns_service_error(client, monkeypatch):
    from app import app
    app.extensions.pop("detection_store")
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
    assert client.post("/api/detections", json=CONFIG, headers=AUTH).status_code == 503


def test_expired_lease_cannot_be_renewed(backend):
    task = queued(backend)
    backend.update(task["id"], lambda row: row.update(lease_until=time.time() - 1))
    assert backend.heartbeat(task["id"])["status"] == "interrupted"


def test_live_request_cancelled_before_completion(backend, upstream):
    url, handler = upstream
    handler.delay = 3
    task = queued(backend)
    def cancel():
        time.sleep(0.5)
        backend.finish(task["id"], "cancelled", "user_cancelled")
    thread = threading.Thread(target=cancel)
    thread.start()
    started = time.monotonic()
    result = worker.run_probe(backend, task["id"], {
        "base_url": url, "api_key": "key", "model": "alias", "prompt": "challenge", "timeout": 5,
    }, time.time() + 10)
    thread.join()
    assert result is None
    assert backend.get(task["id"])["status"] == "cancelled"
    assert time.monotonic() - started < 2


def test_hostname_allowlist(backend, monkeypatch):
    monkeypatch.setenv("UPSTREAM_ALLOWED_HOSTS", "api.example.com")
    assert responses_url("https://api.example.com") == "https://api.example.com/v1/responses"
    with pytest.raises(ValueError, match="UPSTREAM_ALLOWED_HOSTS"):
        responses_url("https://api.example.com.evil.test")


def test_simple_api_four_fields_end_to_end(client, backend, upstream):
    url, handler = upstream
    response = client.post('/api', json={"url": url, "key": "upstream-secret", "model": "alias", "reasoning_effort": "high"})
    assert response.status_code == 202
    task_id = response.json['id']
    assert response.headers['Location'] == f'/api?id={task_id}'
    assert 'upstream-secret' not in response.text
    assert 'public_access' not in response.text
    worker.run_task(backend, backend.claim())
    result = client.get(response.headers['Location'])
    assert result.status_code == 200
    assert result.json['status'] == 'completed'
    assert result.json['reasoning_effort'] == 'high'
    assert len(handler.requests) == 3
    assert all(body['reasoning'] == {"effort": "high"} for _, body, _ in handler.requests)
    assert result.headers['Cache-Control'] == 'no-store'


def test_simple_api_cancel_and_private_task_isolation(client, backend):
    private = backend.submit(CONFIG)
    assert client.get(f"/api?id={private['id']}").status_code == 404
    assert client.delete(f"/api?id={private['id']}").status_code == 404
    response = client.post('/api', json={"url": CONFIG['base_url'], "key": CONFIG['api_key'], "model": "alias", "reasoning_effort": "medium"})
    assert client.delete(response.json['status_url']).json['status'] == 'cancelled'
    assert client.get('/api').status_code == 404


@pytest.mark.parametrize('effort', [None, '', 'invalid', 3, [], {}])
def test_simple_api_rejects_invalid_effort(client, effort):
    assert client.post('/api', json={"url": CONFIG['base_url'], "key": CONFIG['api_key'], "model": "alias", "reasoning_effort": effort}).status_code == 400


def test_simple_api_requires_only_documented_fields(client):
    assert client.post('/api', json=CONFIG).status_code == 400
    assert client.post('/api', json={"url": CONFIG['base_url'], "key": CONFIG['api_key'], "model": "alias"}).status_code == 400
