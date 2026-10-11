import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from detection_store import QUEUE
from monitor_store import DEFAULT, DEFAULT_DASHBOARD, DUE, MonitorStore
from test_detections import backend, client, worker, success

CONFIG = {"url":"https://example.com/v1", "key":"monitor-upstream-secret", "model":"alias",
          "reasoning_effort":"high", "interval_seconds":1800}


def create(client):
    response = client.post('/status/api/monitors', json=CONFIG)
    assert response.status_code == 201
    return response.json


def test_shared_default_across_clients(client, backend):
    from app import app
    other = app.test_client()
    url = '/status/api/monitors/default'
    assert other.get(url).json is None
    saved = client.post(url, json=CONFIG)
    assert saved.status_code == 200
    record = saved.json
    assert other.get(url).json == record
    assert other.get(url).headers['Cache-Control'] == 'no-store'
    assert 'encrypted_key' not in record
    assert CONFIG['key'] not in saved.text
    client.patch('/status/api/monitors/' + record['id'], json={'model':'shared-model'})
    assert other.get(url).json['model'] == 'shared-model'
    # A stale first-time form must neither overwrite nor create a second monitor.
    assert other.post(url, json=CONFIG).json['model'] == 'shared-model'
    assert len(list(backend.redis.scan_iter(match='modeltrace:monitor:*'))) == 1


def test_default_adopts_oldest_existing_monitor(client, backend):
    first = create(client)
    create(client)
    assert client.get('/status/api/monitors/default').json['id'] == first['id']
    assert backend.redis.get(DEFAULT) == first['id']


def test_concurrent_default_creation_uses_one_monitor(backend):
    from monitor_api import validate_config
    monitors = MonitorStore(backend)
    config = validate_config(CONFIG)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: monitors.default(config), range(8)))
    assert len({record['id'] for record in records}) == 1
    assert len(list(backend.redis.scan_iter(match='modeltrace:monitor:*'))) == 1


def test_home_opens_monitoring_and_old_index_is_removed(client):
    response = client.get('/')
    assert response.status_code == 302
    assert response.headers['Location'] == '/status'
    assert client.get('/', follow_redirects=True).status_code == 200
    assert client.get('/index').status_code == 404
    assert client.get('/static/app.js').status_code == 404


def test_shared_dashboard_across_browsers(client, backend):
    from app import app
    other = app.test_client()
    first = client.get('/status/api/dashboards/default')
    assert first.status_code == 200 and first.json['monitors'] == []
    board = first.json['id']
    assert other.get('/status/api/dashboards/default').json['id'] == board
    created = add_monitor(client, board, name='共享供应商 A', model='model-a')
    add_monitor(other, board, name='共享供应商 B', model='model-b', url='https://other.example/v1')
    reply = other.get('/status/api/dashboards/default')
    assert [item['model'] for item in reply.json['monitors']] == ['model-a', 'model-b']
    assert reply.headers['Cache-Control'] == 'no-store'
    assert 'encrypted_key' not in reply.text and CONFIG['key'] not in reply.text
    other.patch('/status/api/monitors/' + created['id'], json={'enabled': True})
    assert client.get('/status/api/dashboards/default').json['monitors'][0]['enabled']
    assert len(list(backend.redis.scan_iter('modeltrace:dashboard:*'))) == 1


def test_shared_dashboard_adopts_existing_panel_and_preserves_state(client, backend):
    board = dashboard(client)
    first = add_monitor(client, board, model='model-a')
    second = add_monitor(client, board, model='model-b')
    another_board = dashboard(client)
    add_monitor(client, another_board, model='other-panel')
    store = MonitorStore(backend)
    store.update(first['id'], {'enabled': True})
    store.tick(first['id'])
    before = [store.get(item['id']) for item in (first, second)]
    shared = client.get('/status/api/dashboards/default').json
    assert shared['id'] == board
    assert [item['id'] for item in shared['monitors']] == [first['id'], second['id']]
    assert [store.get(item['id']) for item in (first, second)] == before
    assert backend.redis.get(DEFAULT_DASHBOARD) == board
    assert store.get_dashboard(another_board)['monitor_ids']


def test_shared_dashboard_adopts_standalone_legacy_monitors(client, backend):
    first, second = create(client), create(client)
    store = MonitorStore(backend)
    before = [store.get(item['id']) for item in (first, second)]
    shared = client.get('/status/api/dashboards/default').json
    assert [item['id'] for item in shared['monitors']] == [first['id'], second['id']]
    assert [store.get(item['id']) for item in (first, second)] == before


def test_concurrent_shared_dashboard_initialization_uses_one_panel(backend):
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: MonitorStore(backend).default_dashboard(), range(8)))
    assert len({record['id'] for record in records}) == 1
    assert len(list(backend.redis.scan_iter('modeltrace:dashboard:*'))) == 1


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


def dashboard(client, monitor_id=None):
    response = client.post('/status/api/dashboards', json={'monitor_id': monitor_id} if monitor_id else {})
    assert response.status_code == 201
    return response.json['id']


def add_monitor(client, dashboard_id, **changes):
    response = client.post(f'/status/api/dashboards/{dashboard_id}/monitors', json={**CONFIG, **changes})
    assert response.status_code == 201, response.json
    return response.json


def test_dashboard_adopts_legacy_monitor_without_changing_it(client, backend):
    legacy = create(client)
    store = MonitorStore(backend)
    store.update(legacy['id'], {'enabled': True})
    store.tick(legacy['id'])
    before = store.get(legacy['id'])
    # Simulate a stored record created before names were introduced.
    before.pop('name')
    backend.redis.set(store.key(legacy['id']), json.dumps(before))
    board = dashboard(client, legacy['id'])
    restored = MonitorStore(backend).get_dashboard(board)
    assert restored['monitor_ids'] == [legacy['id']]
    assert store.get(legacy['id']) == before
    reply = client.get(f'/status/api/dashboards/{board}')
    assert reply.json['monitors'][0]['id'] == legacy['id']
    assert reply.json['monitors'][0]['active_task_id'] == before['active_task_id']
    assert reply.headers['Cache-Control'] == 'no-store'
    assert 'encrypted_key' not in reply.text and CONFIG['key'] not in reply.text


def test_multiple_providers_keep_credentials_history_and_schedule_separate(client, backend):
    board = dashboard(client)
    first = add_monitor(client, board, name='供应商 A / 模型 1', model='model-1', interval_seconds=60)
    second = add_monitor(client, board, name='供应商 A / 模型 2', model='model-2')
    third = add_monitor(client, board, name='供应商 B', model='model-1', url='https://other.example/v1', key='other-secret')
    store = MonitorStore(backend)
    for item in (first, second, third):
        store.update(item['id'], {'enabled': True})
        store.tick(item['id'])
    assert backend.redis.llen(QUEUE) == 3
    first_task, second_task, third_task = [backend.claim() for _ in range(3)]
    assert [backend.credential(item['id']) for item in (first_task, second_task, third_task)] == [CONFIG['key'], CONFIG['key'], 'other-secret']
    assert [item['requested_model'] for item in (first_task, second_task, third_task)] == ['model-1', 'model-2', 'model-1']
    assert third_task['base_url'] == 'https://other.example/v1/responses'
    client.patch('/status/api/monitors/' + second['id'], json={'enabled': False, 'name': '暂停这一项'})
    worker.run_task(backend, first_task, success)
    backend.finish(second_task['id'], 'failed', 'upstream_error')
    worker.run_task(backend, third_task, success)
    for item in (first, second, third):
        store.tick(item['id'])
    records = [store.get(item['id']) for item in (first, second, third)]
    assert [record['history'][0]['id'] for record in records] == [first_task['id'], second_task['id'], third_task['id']]
    assert records[0]['next_run_at'] < time.time() + 61
    assert records[1]['next_run_at'] is None and not records[1]['enabled']
    assert records[2]['enabled'] and records[2]['next_run_at'] > time.time() + 1790
    public = client.get(f'/status/api/dashboards/{board}').json
    assert len(public['monitors']) == 3
    assert [item['latest']['status'] for item in public['monitors']] == ['completed', 'failed', 'completed']
    assert all('history' not in item and 'samples' not in item for item in public['monitors'])
    assert not any(secret in json.dumps(public) for secret in ('encrypted_key', 'other-secret', CONFIG['key']))


def test_reuse_connection_creates_independent_monitor_without_disclosing_key(client, backend):
    board = dashboard(client)
    original = add_monitor(client, board)
    copy = {key: value for key, value in CONFIG.items() if key != 'key'}
    copy.update(source_monitor_id=original['id'], model='another-model', name='第二个模型')
    response = client.post(f'/status/api/dashboards/{board}/monitors', json=copy)
    assert response.status_code == 201
    assert response.json['id'] != original['id']
    assert not response.json['enabled'] and response.json['history'] == []
    assert CONFIG['key'] not in response.text and 'encrypted_key' not in response.text
    store = MonitorStore(backend)
    saved = store.get(response.json['id'])
    assert backend.cipher.decrypt(saved['encrypted_key'].encode()).decode() == CONFIG['key']
    client.patch('/status/api/monitors/' + original['id'], json={'key': 'changed-original'})
    assert backend.cipher.decrypt(store.get(saved['id'])['encrypted_key'].encode()).decode() == CONFIG['key']
    assert store.get(original['id'])['model'] == CONFIG['model']


def test_reuse_requires_new_key_for_changed_url_and_membership(client, backend):
    first_board, other_board = dashboard(client), dashboard(client)
    original = add_monitor(client, first_board)
    config = {key: value for key, value in CONFIG.items() if key != 'key'}
    config['source_monitor_id'] = original['id']
    assert client.post(f'/status/api/dashboards/{other_board}/monitors', json=config).status_code == 404
    config['url'] = 'https://other.example/v1'
    assert client.post(f'/status/api/dashboards/{first_board}/monitors', json=config).status_code == 400
    config['key'] = 'other-provider-key'
    assert client.post(f'/status/api/dashboards/{first_board}/monitors', json=config).status_code == 201
    assert len(client.get(f'/status/api/dashboards/{other_board}').json['monitors']) == 0
    assert client.get('/status/api/dashboards').status_code == 405
    assert client.get('/status/api/monitors').status_code == 405
    assert client.get('/status/api/dashboards/' + '0' * 32).status_code == 404
    assert client.post('/status/api/dashboards', json={'monitor_id': '0' * 32}).status_code == 404


def test_concurrent_additions_do_not_lose_membership(backend):
    store = MonitorStore(backend)
    board = store.create_dashboard()
    config = {'base_url': CONFIG['url'], 'api_key': CONFIG['key'], 'model': CONFIG['model'],
              'reasoning_effort': 'high', 'interval_seconds': 60}
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: MonitorStore(backend).add_to_dashboard(board['id'], config), range(8)))
    assert set(store.get_dashboard(board['id'])['monitor_ids']) == {item['id'] for item in created}
    assert all(store.get(item['id']) for item in created)


@pytest.mark.parametrize('name', [None, 123, 'x' * 81])
def test_invalid_monitor_name(client, name):
    assert client.post('/status/api/monitors', json={**CONFIG, 'name': name}).status_code == 400


def test_full_dashboard_does_not_create_orphan_monitors(client, backend):
    board = dashboard(client)
    store = MonitorStore(backend)
    record = store.get_dashboard(board)
    record['monitor_ids'] = ['0' * 32] * 100
    backend.redis.set(store.dashboard_key(board), json.dumps(record))
    before = set(backend.redis.scan_iter('modeltrace:monitor:*'))
    assert client.post(f'/status/api/dashboards/{board}/monitors', json=CONFIG).status_code == 400
    assert set(backend.redis.scan_iter('modeltrace:monitor:*')) == before


@pytest.mark.parametrize('url', ['https://other.example/v1', 'http://example.com/v1', 'https://example.com/other'])
def test_edit_url_requires_new_key_without_mutating_monitor(client, backend, url):
    record = create(client)
    store = MonitorStore(backend)
    before = store.get(record['id'])
    response = client.patch('/status/api/monitors/' + record['id'], json={'url': url, 'enabled': True})
    assert response.status_code == 400
    assert store.get(record['id']) == before
    assert backend.redis.zcard(DUE) == 0
    response = client.patch('/status/api/monitors/' + record['id'], json={'url': url, 'key': 'replacement'})
    assert response.status_code == 200
    store.update(record['id'], {'run_requested': True})
    store.tick(record['id'])
    task = backend.claim()
    assert backend.credential(task['id']) == 'replacement'
    assert task['base_url'] == response.json['url']


def test_equivalent_url_keeps_key_but_stale_editor_cannot_redirect_it(client, backend):
    record = create(client)
    url = '/status/api/monitors/' + record['id']
    assert client.patch(url, json={'url': CONFIG['url'] + '/responses/'}).status_code == 200
    assert client.patch(url, json={'url': 'https://new.example/v1', 'key': 'new-key'}).status_code == 200
    # A previously opened editor still sends its original URL with no key.
    assert client.patch(url, json={'url': CONFIG['url'], 'model': 'stale-model'}).status_code == 400
    saved = MonitorStore(backend).get(record['id'])
    assert saved['model'] == CONFIG['model']
    assert backend.cipher.decrypt(saved['encrypted_key'].encode()).decode() == 'new-key'


def test_legacy_bookmark_reuses_shared_panel_and_keeps_new_items(client, backend):
    shared = client.get('/status/api/dashboards/default').json['id']
    legacy = create(client)
    before = MonitorStore(backend).get(legacy['id'])
    assert dashboard(client, legacy['id']) == shared
    added = add_monitor(client, shared, model='second')
    assert dashboard(client, legacy['id']) == shared
    assert [m['id'] for m in client.get('/status/api/dashboards/default').json['monitors']] == [legacy['id'], added['id']]
    assert MonitorStore(backend).get(legacy['id']) == before
    assert len(list(backend.redis.scan_iter('modeltrace:dashboard:*'))) == 1


def test_concurrent_legacy_adoption_does_not_duplicate_or_lose_members(backend):
    from monitor_api import validate_config
    store = MonitorStore(backend)
    shared = store.default_dashboard()
    records = [store.create(validate_config(CONFIG)) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        boards = list(pool.map(lambda item: MonitorStore(backend).create_dashboard(item['id']), records * 2))
    assert {board['id'] for board in boards} == {shared['id']}
    ids = store.get_dashboard(shared['id'])['monitor_ids']
    assert len(ids) == len(records)
    assert set(ids) == {record['id'] for record in records}


def test_status_assets_use_content_addressed_urls(client):
    import hashlib
    import re
    from pathlib import Path
    response = client.get('/status')
    assert response.headers['Cache-Control'] == 'no-store'
    urls = re.findall(r'(?:src|href)="(/status/assets/[^"]+)"', response.text)
    assert len(urls) == 2
    for url in urls:
        filename = url.rsplit('/', 1)[1]
        content = (Path(__file__).resolve().parents[1] / 'static' / filename).read_bytes()
        digest = hashlib.sha256(content).hexdigest()[:16]
        assert url == f'/status/assets/{digest}/{filename}'
        asset = client.get(url)
        assert asset.status_code == 200 and asset.data == content
        assert 'immutable' in asset.headers['Cache-Control']
        assert client.get(f'/status/assets/stale/{filename}').status_code == 404
    assert client.get('/status/assets/stale/app.py').status_code == 404
