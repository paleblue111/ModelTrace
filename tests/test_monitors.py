import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from detection_store import QUEUE
from monitor_store import DUE, MonitorStore
from test_detections import backend, client, worker, success

CONFIG = {"url":"https://example.com/v1", "key":"monitor-upstream-secret", "model":"alias",
          "reasoning_effort":"high", "interval_seconds":1800}


def create(client):
    response = client.post('/status/api/monitors', json=CONFIG)
    assert response.status_code == 201
    return response.json


def test_page_and_settings_never_disclose_key(client, backend):
    assert client.get('/status').status_code == 200
    record = create(client)
    assert not record['enabled']
    assert record['has_key']
    assert 'monitor-upstream-secret' not in json.dumps(record)
    assert 'encrypted_key' not in record
    raw = backend.redis.get(MonitorStore.key(record['id']))
    assert 'monitor-upstream-secret' not in raw
    assert 'encrypted_key' in raw
    reply = client.get('/status/api/monitors/' + record['id'])
    assert reply.headers['Cache-Control'] == 'no-store'
    assert reply.json['history'] == []


def test_server_runs_and_persists_history_without_browser(client, backend):
    monitor = create(client)
    url = '/status/api/monitors/' + monitor['id']
    client.patch(url, json={'enabled':True})
    # Only the server is used below; no browser polling drives scheduling.
    monitors = MonitorStore(backend)
    monitors.tick(monitor['id'])
    active = monitors.get(monitor['id'])['active_task_id']
    assert active and backend.redis.llen(QUEUE) == 1
    task = backend.claim()
    worker.run_task(backend, task, lambda *args: success())
    monitors.tick(monitor['id'])
    saved = MonitorStore(backend).get(monitor['id'])
    assert saved['active_task_id'] is None
    assert saved['history'][0]['status'] == 'completed'
    assert saved['total_runs'] == 1
    assert saved['next_run_at'] > time.time() + 1790
    assert MonitorStore(backend).public(saved)['history'][0]['result']['used_outputs'] == 3
    # No overlap and no premature next request.
    monitors.tick(monitor['id'])
    assert backend.redis.llen(QUEUE) == 0
    saved['next_run_at'] = time.time() - 1
    backend.redis.set(monitors.key(saved['id']), json.dumps(saved))
    monitors.tick(saved['id'])
    assert backend.redis.llen(QUEUE) == 1


def test_concurrent_schedulers_create_exactly_one_task(client, backend):
    monitor = create(client)
    client.post('/status/api/monitors/' + monitor['id'] + '/run')
    monitors = MonitorStore(backend)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: monitors.tick(monitor['id']), range(8)))
    assert backend.redis.llen(QUEUE) == 1
    assert monitors.get(monitor['id'])['active_task_id']
    assert client.post('/status/api/monitors/' + monitor['id'] + '/run').status_code == 409


def test_pause_preserves_active_task_but_disables_next_round(client, backend):
    monitor = create(client)
    url = '/status/api/monitors/' + monitor['id']
    client.patch(url, json={'enabled':True})
    monitors = MonitorStore(backend)
    monitors.tick(monitor['id'])
    task = backend.claim()
    client.patch(url, json={'enabled':False})
    assert backend.get(task['id'])['status'] == 'running'
    worker.run_task(backend, task, lambda *args: success())
    monitors.tick(monitor['id'])
    saved = monitors.get(monitor['id'])
    assert not saved['enabled'] and saved['next_run_at'] is None
    assert backend.redis.zscore(DUE, monitor['id']) is None
    monitors.tick(monitor['id'])
    assert backend.claim() is None


def test_settings_keep_key_when_omitted_and_apply_next_round(client, backend):
    monitor = create(client)
    url = '/status/api/monitors/' + monitor['id']
    monitors = MonitorStore(backend)
    before = monitors.get(monitor['id'])['encrypted_key']
    response = client.patch(url, json={'model':'new-alias', 'reasoning_effort':'low', 'interval_seconds':60})
    assert response.status_code == 200
    assert monitors.get(monitor['id'])['encrypted_key'] == before
    client.post(url + '/run')
    monitors.tick(monitor['id'])
    task = backend.claim()
    assert task['requested_model'] == 'new-alias' and task['reasoning_effort'] == 'low'
    assert backend.credential(task['id']) == CONFIG['key']
    client.patch(url, json={'key':'new-secret'})
    assert backend.credential(task['id']) == CONFIG['key']
    assert backend.cipher.decrypt(monitors.get(monitor['id'])['encrypted_key'].encode()).decode() == 'new-secret'


def test_partial_result_and_expired_job_do_not_block_monitor(client, backend):
    monitor = create(client)
    monitors = MonitorStore(backend)
    monitors.update(monitor['id'], {'enabled':True})
    monitors.tick(monitor['id'])
    task = backend.claim()
    calls = []
    def probe(*args):
        calls.append(1)
        return success() if len(calls) < 3 else {'ok':True,'text':'invalid'}
    worker.run_task(backend, task, probe)
    monitors.tick(monitor['id'])
    saved = monitors.get(monitor['id'])
    assert saved['history'][0]['status'] == 'partial'
    assert saved['history'][0]['result']['insufficient_samples']
    monitors.update(monitor['id'], {'run_requested':True})
    monitors.tick(monitor['id'])
    backend.redis.delete(backend.key(monitors.get(monitor['id'])['active_task_id']))
    monitors.tick(monitor['id'])
    saved = monitors.get(monitor['id'])
    assert saved['active_task_id'] is None and saved['next_run_at']
    assert saved['last_error']


def test_queue_backpressure_and_history_limit(client, backend):
    monitor = create(client)
    monitors = MonitorStore(backend)
    monitors.update(monitor['id'], {'enabled':True})
    backend.queue_limit = 1
    backend.redis.rpush(QUEUE, 'occupied')
    monitors.tick(monitor['id'])
    assert monitors.get(monitor['id'])['last_error']
    assert monitors.get(monitor['id'])['active_task_id'] is None
    backend.redis.delete(QUEUE)
    for _ in range(22):
        monitors.update(monitor['id'], {'run_requested':True})
        monitors.tick(monitor['id'])
        task = backend.claim()
        backend.finish(task['id'], 'failed', 'upstream_error')
        monitors.tick(monitor['id'])
    saved = monitors.get(monitor['id'])
    assert len(saved['history']) == 20
    assert saved['total_runs'] == 22


@pytest.mark.parametrize('changes', [{'interval_seconds':0}, {'interval_seconds':True}, {'interval_seconds':604801},
                                    {'key':''}, {'reasoning_effort':None}, {'reasoning_effort':'invalid'}, {'enabled':'yes'}])
def test_invalid_settings(client, changes):
    monitor = create(client)
    assert client.patch('/status/api/monitors/' + monitor['id'], json=changes).status_code == 400


def test_allowlist_does_not_break_pause(client, backend, monkeypatch):
    monitor = create(client)
    monkeypatch.setenv('UPSTREAM_ALLOWED_HOSTS', 'different-allowed-host.example')
    assert client.patch('/status/api/monitors/' + monitor['id'], json={'enabled':False}).status_code == 200
