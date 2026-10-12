import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from admin_auth import COOKIE, session_key
from detection_store import PUBLIC_EXPIRY, PUBLIC_INDEX, PUBLIC_QUEUE, PUBLIC_RUNNING, PUBLIC_VISIBLE_INDEX, QUEUE
from monitor_api import validate_config
from monitor_store import MonitorStore, VISIBILITY_VERSION
from public_api import PublicStore
from site_settings import SiteSettings
from test_detections import CONFIG, backend, client, success, upstream, worker

PUBLIC = {"url": CONFIG["base_url"], "key": CONFIG["api_key"], "model": CONFIG["model"], "reasoning_effort": "high"}
MONITOR = {**PUBLIC, "interval_seconds": 1800}


def login(client):
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'})
    assert reply.status_code == 200
    return {'X-CSRF-Token': reply.json['csrf']}


def post_public(client, ip='192.0.2.1', **changes):
    return client.post('/public/api/detections', json={**PUBLIC, **changes}, environ_overrides={'REMOTE_ADDR': ip})


def test_admin_required_for_every_monitor_and_admin_endpoint(client, backend):
    saved = MonitorStore(backend).create(validate_config(MONITOR))
    urls = ['/status/api/dashboards/default', '/status/api/monitors/default', '/status/api/monitors/' + saved['id'],
            '/admin/api/site', '/admin/api/detections', '/admin/api/session']
    for url in urls:
        assert client.get(url).status_code == 401, url
    for method, url, body in [('post','/status/api/monitors',MONITOR), ('patch','/status/api/monitors/' + saved['id'],{'enabled':True}),
                              ('delete','/status/api/monitors/' + saved['id'],{}), ('post','/status/api/monitors/' + saved['id'] + '/run',{}),
                              ('patch','/admin/api/site',{'title':'attack'})]:
        assert getattr(client, method)(url, json=body).status_code == 401
    assert not MonitorStore(backend).get(saved['id'])['enabled']
    assert client.get('/admin').status_code == 302
    assert client.get('/status').status_code == 200
    assert 'monitor-add' not in client.get('/status').text


def test_login_csrf_logout_expiry_rotation_and_cookie(client, backend, monkeypatch):
    assert client.post('/admin/api/login', json={'token':'wrong'}).status_code == 401
    assert client.post('/admin/api/login', json={'token':'service-test-token'}, headers={'Origin':'https://evil.example'}).status_code == 403
    csrf = login(client)
    cookie = client.get_cookie(COOKIE)
    assert cookie.http_only and cookie.same_site == 'Lax'
    sid = cookie.value
    assert 28790 <= backend.redis.ttl(session_key(sid)) <= 28800
    assert client.get('/admin').status_code == 200
    assert client.get('/status/api/dashboards/default').status_code == 200
    assert client.patch('/admin/api/site', json={'title':'new'}).status_code == 403
    assert client.patch('/admin/api/site', json={'title':'new'}, headers={**csrf, 'Origin':'https://evil.example'}).status_code == 403
    assert client.patch('/admin/api/site', json={'title':'new'}, headers=csrf).status_code == 200
    assert client.post('/admin/api/logout', json={}, headers=csrf).status_code == 200
    assert backend.redis.get(session_key(sid)) is None
    assert client.get('/status/api/dashboards/default').status_code == 401
    login(client)
    backend.redis.delete(session_key(client.get_cookie(COOKIE).value))
    assert client.get('/admin/api/site').status_code == 401
    login(client)
    monkeypatch.setenv('MODELTRACE_API_TOKEN', 'rotated-token')
    assert client.get('/admin/api/site').status_code == 401


def test_secure_cookie_and_login_rate(client):
    reply = client.post('/admin/api/login', json={'token':'service-test-token'}, base_url='https://example.test')
    assert reply.status_code == 200 and 'Secure;' in reply.headers['Set-Cookie']
    for _ in range(5):
        assert client.post('/admin/api/login', json={'token':'wrong'}, environ_overrides={'REMOTE_ADDR':'192.0.2.77'}).status_code == 401
    reply = client.post('/admin/api/login', json={'token':'service-test-token'}, environ_overrides={'REMOTE_ADDR':'192.0.2.77'})
    assert reply.status_code == 429 and reply.headers['Retry-After']


@pytest.mark.parametrize('host', ['example.test', 'example.test:8443'])
def test_https_proxy_login_origin_session_and_csrf(client, monkeypatch, host):
    from app import app
    from werkzeug.middleware.proxy_fix import ProxyFix

    base_url = 'http://' + host
    headers = {'Origin': 'https://' + host, 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '192.0.2.1'}
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url=base_url, headers=headers)
    assert reply.status_code == 403 and 'TRUSTED_PROXY_HOPS' in reply.json['error']
    assert 'Set-Cookie' not in reply.headers

    monkeypatch.setattr(app, 'wsgi_app', ProxyFix(app.wsgi_app, x_for=1, x_proto=1))
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url=base_url, headers=headers)
    assert reply.status_code == 200 and 'Secure;' in reply.headers['Set-Cookie']
    csrf = reply.json['csrf']
    assert client.get('/admin', base_url=base_url, headers=headers).status_code == 200
    assert client.get('/admin/api/session', base_url=base_url, headers=headers).json['csrf'] == csrf
    assert client.patch('/admin/api/site', json={'title': 'proxy login works'}, base_url=base_url,
                        headers={**headers, 'X-CSRF-Token': csrf}).status_code == 200
    rejected = client.patch('/admin/api/site', json={'title': 'blocked'}, base_url=base_url,
                            headers={**headers, 'Origin': 'https://other.example', 'X-CSRF-Token': csrf})
    assert rejected.status_code == 403 and '请求来源无效' in rejected.json['error']
    assert client.get('/admin/api/site', base_url=base_url, headers=headers).json['title'] == 'proxy login works'
    rejected = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url=base_url,
                           headers={**headers, 'Origin': 'https://other.example'})
    assert rejected.status_code == 403
    # ProxyFix deliberately does not trust X-Forwarded-Host; the proxy must preserve Host itself.
    rejected = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url='http://internal.test:10001',
                           headers={**headers, 'X-Forwarded-Host': host})
    assert rejected.status_code == 403
    assert client.post('/admin/api/logout', json={}, base_url=base_url,
                       headers={**headers, 'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/admin/api/session', base_url=base_url, headers=headers).status_code == 401


@pytest.mark.parametrize('origin', ['https://example.test', 'https://example.test:8443'])
def test_browser_proxy_login_without_trusting_forwarded_headers(client, backend, origin):
    base_url = 'http://internal.test:10001'
    headers = {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}
    wrong = client.post('/admin/api/login', json={'token': 'wrong'}, base_url=base_url, headers=headers)
    assert wrong.status_code == 401 and 'Set-Cookie' not in wrong.headers
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url=base_url, headers=headers)
    assert reply.status_code == 200
    assert 'Secure;' in reply.headers['Set-Cookie'] and 'HttpOnly;' in reply.headers['Set-Cookie']
    csrf = reply.json['csrf']
    cookie = client.get_cookie(COOKIE, domain='internal.test')
    assert json.loads(backend.redis.get(session_key(cookie.value)))['origin'] == origin
    assert client.get('/admin', base_url=base_url).status_code == 200
    assert client.patch('/admin/api/site', json={'title': 'browser proxy'}, base_url=base_url,
                        headers={**headers, 'X-CSRF-Token': csrf}).status_code == 200
    assert client.patch('/admin/api/site', json={'title': 'blocked'}, base_url=base_url, headers=headers).status_code == 403
    # Even a claimed same-origin request cannot change the origin bound to this session.
    assert client.patch('/admin/api/site', json={'title': 'blocked'}, base_url=base_url,
                        headers={**headers, 'Origin': 'https://other.example', 'X-CSRF-Token': csrf}).status_code == 403
    assert client.get('/admin/api/site', base_url=base_url).json['title'] == 'browser proxy'
    assert client.post('/admin/api/logout', json={}, base_url=base_url,
                       headers={**headers, 'X-CSRF-Token': csrf}).status_code == 200
    assert backend.redis.get(session_key(cookie.value)) is None
    assert client.get('/admin/api/session', base_url=base_url).status_code == 401


@pytest.mark.parametrize('site', ['same-site', 'cross-site', 'invalid'])
@pytest.mark.parametrize('origin', [None, 'http://localhost'])
def test_fetch_metadata_rejects_cross_origin_login_and_writes(client, site, origin):
    headers = {'Sec-Fetch-Site': site}
    if origin is not None:
        headers['Origin'] = origin
    assert client.post('/admin/api/login', json={'token': 'service-test-token'}, headers=headers).status_code == 403
    csrf = login(client)
    assert client.patch('/admin/api/site', json={'title': 'blocked'}, headers={**headers, **csrf}).status_code == 403
    assert client.post('/admin/api/logout', json={}, headers={**headers, **csrf}).status_code == 403
    assert client.get('/admin/api/session').status_code == 200


@pytest.mark.parametrize('origin', ['', 'null', 'file://example.test', 'https://user:password@example.test',
                                    'https://example.test/path', 'https://example.test:99999', 'https://[invalid',
                                    'https://example.test https://other.example'])
def test_fetch_metadata_does_not_accept_invalid_origin(client, origin):
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'},
                        headers={'Sec-Fetch-Site': 'same-origin', 'Origin': origin})
    assert reply.status_code == 403 and 'Set-Cookie' not in reply.headers


def test_origin_default_ports_are_compared_correctly(client):
    reply = client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url='https://example.test:443',
                        headers={'Origin': 'https://example.test'})
    assert reply.status_code == 200 and 'Secure;' in reply.headers['Set-Cookie']
    assert client.post('/admin/api/logout', json={}, base_url='https://example.test:443',
                       headers={'Origin': 'https://example.test:443', 'X-CSRF-Token': reply.json['csrf']}).status_code == 200
    assert client.post('/admin/api/login', json={'token': 'service-test-token'}, base_url='https://example.test:8443',
                       headers={'Origin': 'https://example.test:443'}).status_code == 403


def test_publication_order_history_and_sanitization(client, backend):
    monitors = MonitorStore(backend)
    first = monitors.create(validate_config({**MONITOR, 'name':'private-name', 'published':True, 'public_name':'公开 A', 'public_order':2}))
    second = monitors.create(validate_config({**MONITOR, 'published':True, 'public_name':'公开 B', 'public_order':1}))
    hidden = monitors.create(validate_config(MONITOR))
    monitors.update(first['id'], {'enabled': True})
    monitors.tick(first['id'])
    task = backend.claim()
    assert not task['public_access']
    worker.run_task(backend, task, success)
    monitors.tick(first['id'])
    home = client.get('/public/api/home')
    assert [item['name'] for item in home.json['monitors']] == ['公开 B', '公开 A']
    detail = client.get('/public/api/monitors/' + first['public_id'])
    assert detail.json['history'][0]['result']['prediction_name']
    for private in (CONFIG['api_key'], CONFIG['base_url'], first['id'], second['id'], task['id'], 'private-name', 'status_url', 'encrypted_key', 'bank_sha256'):
        assert private not in detail.text + home.text
    assert client.get('/public/api/monitors/' + hidden['public_id']).status_code == 404
    assert client.get('/public/api/monitors/' + first['id']).status_code == 404
    assert client.get('/api?id=' + task['id']).status_code == 404
    assert client.delete('/api?id=' + task['id']).status_code == 404
    monitors.update(first['id'], {'published':False})
    assert client.get('/public/api/monitors/' + first['public_id']).status_code == 404
    assert monitors.get(first['id'])['enabled']


def test_public_submission_progress_completion_cleanup_and_private_api(client, backend):
    response = post_public(client)
    assert response.status_code == 202
    public_id = response.json['id']
    task_id = backend.redis.get(backend.public_key(public_id))
    assert task_id != public_id
    assert backend.redis.llen(PUBLIC_QUEUE) == 1 and backend.redis.llen(QUEUE) == 0
    assert backend.credential(task_id) == CONFIG['api_key']
    task = backend.claim()
    assert client.get(response.headers['Location']).json['status'] == 'running'
    worker.run_task(backend, task, success)
    reply = client.get(response.headers['Location'])
    assert reply.json['status'] == 'completed'
    assert backend.redis.get(backend.credential_key(task_id)) is None
    assert backend.redis.zcard(PUBLIC_RUNNING) == 0
    assert backend.redis.ttl(backend.public_key(public_id)) <= backend.retention
    assert client.get('/public/api/detections').json['items'][0]['id'] == public_id
    for secret in (task_id, CONFIG['base_url'], CONFIG['api_key'], 'errors', 'status_url', 'client_hash'):
        assert secret not in reply.text
    assert post_public(client, ip='192.0.2.2', enabled=True).status_code == 400
    assert client.delete(response.headers['Location']).status_code == 405
    legacy = client.post('/api', json=PUBLIC, environ_overrides={'REMOTE_ADDR':'192.0.2.3'})
    assert legacy.status_code == 202
    assert client.get('/public/api/detections').json['total'] == 1
    assert client.get('/api?id=' + public_id).status_code == 404


def test_public_moderation_pagination_and_expiry(client, backend):
    for number in range(23):
        assert post_public(client, ip=f'192.0.2.{number + 1}', model=f'model-{number}').status_code == 202
        task = backend.claim()
        backend.finish(task['id'], 'failed', 'upstream_error')
    first = client.get('/public/api/detections').json
    assert first['total'] == 23 and len(first['items']) == 20 and first['pages'] == 2
    assert first['items'][0]['model'] == 'model-22'
    assert len(client.get('/public/api/detections?page=2').json['items']) == 3
    assert client.get('/public/api/detections?page=0').status_code == 400
    public_id = first['items'][0]['id']
    assert client.patch('/admin/api/detections/' + public_id, json={'hidden':True}).status_code == 401
    csrf = login(client)
    assert client.patch('/admin/api/detections/' + public_id, json={'hidden':True}, headers=csrf).status_code == 200
    assert client.get('/public/api/detections/' + public_id).status_code == 404
    assert client.get('/public/api/detections').json['total'] == 22
    assert client.get('/admin/api/detections').json['total'] == 23
    assert client.patch('/admin/api/detections/' + public_id, json={'hidden':False}, headers=csrf).status_code == 200
    assert client.get('/public/api/detections/' + public_id).status_code == 200
    backend.redis.delete(backend.key(backend.redis.get(backend.public_key(public_id))))
    assert client.get('/public/api/detections').json['total'] == 22
    assert backend.redis.zscore(PUBLIC_INDEX, public_id) is None


def test_limits_atomic_pending_hourly_queue_and_settings(client, backend):
    assert post_public(client).status_code == 202
    assert post_public(client).status_code == 429
    backend.finish(backend.claim()['id'], 'failed', 'upstream_error')
    for _ in range(4):
        assert post_public(client).status_code == 202
        backend.finish(backend.claim()['id'], 'failed', 'upstream_error')
    assert post_public(client).status_code == 429
    csrf = login(client)
    assert client.patch('/admin/api/site', json={'public_enabled':False}, headers=csrf).status_code == 200
    assert post_public(client, ip='192.0.2.2').status_code == 403
    assert client.post('/api', json=PUBLIC, environ_overrides={'REMOTE_ADDR':'192.0.2.3'}).status_code == 403
    assert client.get('/public/api/detections').status_code == 200
    assert client.patch('/admin/api/site', json={'public_hourly_limit':True}, headers=csrf).status_code == 400
    SiteSettings(backend.redis).update({'public_enabled':True, 'public_queue_limit':1})
    assert post_public(client, ip='192.0.2.4').status_code == 202
    assert post_public(client, ip='192.0.2.5').status_code == 429


def test_concurrent_anonymous_submissions_and_admin_priority(backend):
    from detection_store import QueueFull
    public_config = {**CONFIG, 'public_access':True}
    def submit(_):
        try:
            return backend.submit(public_config, client_ip='192.0.2.1')
        except QueueFull:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(item is not None for item in results) == 1
    second = backend.submit(public_config, client_ip='192.0.2.2')
    third = backend.submit(public_config, client_ip='192.0.2.3')
    admin = backend.submit(CONFIG)
    assert backend.claim()['id'] == admin['id']
    first_running, second_running = backend.claim(), backend.claim()
    assert backend.redis.zcard(PUBLIC_RUNNING) == 2
    assert backend.claim() is None
    admin2 = backend.submit(CONFIG)
    assert backend.claim()['id'] == admin2['id']
    backend.finish(first_running['id'], 'failed', 'upstream_error')
    assert backend.claim()['id'] == third['id']
    backend.update(second_running['id'], lambda item: item.update(lease_until=time.time() - 1))
    backend.reap()
    assert backend.redis.zscore(PUBLIC_RUNNING, second_running['id']) is None


def test_legacy_migration_preserves_monitor_state_and_locks_task(client, backend):
    monitors = MonitorStore(backend)
    saved = monitors.create(validate_config(MONITOR))
    monitors.update(saved['id'], {'enabled':True})
    monitors.tick(saved['id'])
    before = monitors.get(saved['id'])
    for field in ('published','public_name','public_order','public_id'):
        before.pop(field)
    backend.redis.set(monitors.key(saved['id']), json.dumps(before))
    task_id = before['active_task_id']
    task = backend.get(task_id)
    task['public_access'] = True
    task.pop('task_class')
    backend.redis.set(backend.key(task_id), json.dumps(task), ex=86400)
    external = backend.submit({**CONFIG, 'public_access':True}, client_ip='192.0.2.8')
    backend.redis.lrem(PUBLIC_QUEUE, 0, external['id'])
    backend.redis.rpush(QUEUE, external['id'])
    # Even the first legacy API request must protect an old monitor task before the homepage opens.
    assert client.delete('/api?id=' + task_id).status_code == 404
    after = monitors.get(saved['id'])
    assert {key: after[key] for key in before} == before
    assert not after['published']
    assert not backend.get(task_id)['public_access']
    assert client.delete('/api?id=' + task_id).status_code == 404
    assert backend.redis.lrange(QUEUE,0,-1) == [task_id]
    assert backend.redis.lrange(PUBLIC_QUEUE,0,-1) == [external['id']]
    monitors.update(saved['id'], {'published':True})
    monitors.migrate_visibility()
    assert monitors.get(saved['id'])['published']
    assert backend.redis.get(VISIBILITY_VERSION)
    worker.run_task(backend, backend.claim(), success)
    monitors.tick(saved['id'])
    assert monitors.get(saved['id'])['history'][0]['status'] == 'completed'


def test_anonymous_network_requests_never_use_admin_private_host_override(client, backend, upstream):
    url, handler = upstream
    assert post_public(client, url=url).status_code == 202
    task = backend.claim()
    worker.run_task(backend, task)
    assert backend.get(task['id'])['status'] == 'failed'
    assert handler.requests == []


def test_expired_public_indexes_are_removed_by_server_reaper(client, backend):
    reply = post_public(client)
    public_id = reply.json['id']
    backend.finish(backend.claim()['id'], 'completed', 'target_reached')
    backend.redis.zadd(PUBLIC_EXPIRY, {public_id: time.time() - 1})
    backend.reap()
    for key in (PUBLIC_EXPIRY, PUBLIC_INDEX, PUBLIC_VISIBLE_INDEX):
        assert backend.redis.zscore(key, public_id) is None
    assert client.get('/public/api/detections/' + public_id).status_code == 404
    assert client.get('/public/api/detections').json['total'] == 0


def test_untrusted_forwarding_headers_do_not_bypass_ip_limit(client, backend):
    first = client.post('/public/api/detections', json=PUBLIC, headers={'X-Forwarded-For':'192.0.2.1'})
    assert first.status_code == 202
    second = client.post('/public/api/detections', json=PUBLIC, headers={'X-Forwarded-For':'192.0.2.2'})
    assert second.status_code == 429
    reply = client.post('/admin/api/login', json={'token':'service-test-token'}, headers={'X-Forwarded-Proto':'https'})
    assert 'Secure;' not in reply.headers['Set-Cookie']


def test_old_unreferenced_tasks_cannot_leak_admin_history(client, backend):
    task = backend.submit({**CONFIG, 'public_access':True}, client_ip='192.0.2.1')
    record = backend.get(task['id'])
    record.pop('task_class')
    backend.redis.set(backend.key(task['id']), json.dumps(record), ex=86400)
    assert client.get('/api?id=' + task['id']).status_code == 404
    assert client.get('/api/detections/' + task['id'], headers={'Authorization':'Bearer service-test-token'}).status_code == 200
    assert backend.get(task['id'])['status'] == 'queued'
    assert backend.claim()['id'] == task['id']


def test_monitor_scheduler_respects_shared_queue_capacity(client, backend):
    backend.queue_limit = 1
    public = post_public(client)
    assert public.status_code == 202
    monitors = MonitorStore(backend)
    monitor = monitors.create(validate_config(MONITOR))
    monitors.update(monitor['id'], {'enabled':True})
    monitors.tick(monitor['id'])
    assert backend.redis.llen(QUEUE) == 0
    assert monitors.get(monitor['id'])['last_error']
    backend.finish(backend.claim()['id'], 'failed', 'upstream_error')
    monitors.update(monitor['id'], {'enabled':True})
    monitors.tick(monitor['id'])
    assert backend.redis.llen(QUEUE) == 1
