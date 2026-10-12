"""Real browser check for the public site and cookie-authenticated administrator UI."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
os.environ['CREDENTIAL_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['MODELTRACE_API_TOKEN'] = 'public-browser-admin-token'
os.environ['ASYNC_ONLY'] = 'true'
os.environ['TRUSTED_PROXY_HOPS'] = '0'
proxy_https = os.getenv('BROWSER_HTTPS_PROXY') == '1'

import fakeredis
from flask import request
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app import app
from detection_store import DetectionStore
from detection_worker import run_task
from monitor_api import validate_config
from monitor_store import MonitorStore

backend = DetectionStore(fakeredis.FakeRedis(decode_responses=True))
app.extensions['detection_store'] = backend
monitors = MonitorStore(backend)
delayed = {'id': None}
delay_started, delay_release = threading.Event(), threading.Event()
proxy_requests = []


@app.after_request
def delay_public_detail(response):
    if proxy_https and request.path.startswith('/admin/api/') and request.method in {'POST', 'PATCH'}:
        proxy_requests.append({'path': request.path, 'site': request.headers.get('Sec-Fetch-Site'),
                               'origin': request.headers.get('Origin'), 'secure': request.is_secure,
                               'host': request.host, 'status': response.status_code})
    if delayed['id'] and request.path == '/public/api/detections/' + delayed['id']:
        delayed['id'] = None
        delay_started.set()
        delay_release.wait(5)
    return response


config = {'url':'https://private-provider.example/v1', 'key':'private-administrator-key', 'model':'admin-model',
          'reasoning_effort':'high', 'interval_seconds':1800}
first = monitors.create(validate_config({**config, 'name':'private administrator label'}))
second = monitors.create(validate_config({**config, 'name':'second private label', 'model':'admin-model-2'}))
backend.redis.set('modeltrace:scheduler:heartbeat', time.time(), ex=600)


def internal_http(environ, start_response):
    # Real browser HTTPS, with the upstream scheme and Host rewritten as by a proxy.
    upstream = dict(environ, HTTP_HOST='internal.test:10001', SERVER_NAME='internal.test', SERVER_PORT='10001')
    upstream['wsgi.url_scheme'] = 'http'
    return app(upstream, start_response)


server = make_server('127.0.0.1', 0, internal_http if proxy_https else app, threaded=True,
                     ssl_context='adhoc' if proxy_https else None)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f'{"https" if proxy_https else "http"}://127.0.0.1:{server.server_port}'
output = Path(os.getenv('STATUS_SCREENSHOT_DIR', '/tmp/modeltrace-public-preview'))
output.mkdir(parents=True, exist_ok=True)


def complete(task):
    numbers = ','.join(str((index * 73) % 355 + 1) for index in range(330))
    run_task(backend, task, lambda *args: {'ok':True, 'text':numbers})


try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors = []
        admin_context = browser.new_context(viewport={'width':1440,'height':1000}, ignore_https_errors=proxy_https)
        guest_context = browser.new_context(viewport={'width':1440,'height':1000}, ignore_https_errors=proxy_https)
        admin = admin_context.new_page()
        guest = guest_context.new_page()
        for page in (admin, guest):
            page.on('pageerror', lambda error: errors.append(str(error)))
        guest.goto(base + '/')
        expect(guest.locator('#monitor-list .monitor-tile')).to_have_count(0)
        expect(guest.locator('#public-test-form')).to_be_visible()
        assert guest.locator('#monitor-add').count() == 0
        assert guest.locator('#settings-open').count() == 0
        assert guest.locator('[data-action="delete"]').count() == 0
        admin.goto(base + '/admin')
        expect(admin.locator('#login-form')).to_be_visible()
        admin.locator('#login-token').fill('wrong-token')
        admin.locator('#login-submit').click()
        expect(admin.locator('#login-error')).to_contain_text('不正确')
        admin.locator('#login-token').fill('public-browser-admin-token')
        admin.locator('#login-submit').click()
        expect(admin.locator('#monitor-add')).to_be_enabled()
        expect(admin.locator('.monitor-tile')).to_have_count(2)
        if proxy_https:
            cookie = next(item for item in admin_context.cookies() if item['name'] == 'modeltrace_admin')
            assert cookie['secure'] and cookie['httpOnly'] and cookie['sameSite'] == 'Lax'
            assert 'modeltrace_admin' not in admin.evaluate('document.cookie')
        assert not any('public-browser-admin-token' in storage for storage in admin.evaluate('[JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]'))
        expect(admin.locator('#site-title')).to_have_value('每一次调用，都有迹可循。')
        admin.locator('#site-title').fill('公开模型身份观测站')
        admin.locator('#site-description').fill('查看管理员精选与所有人的单次检测结果。')
        admin.locator('#site-announcement').fill('<img src=x onerror="window.injected = true">欢迎查看公开结果')
        admin.locator('#site-save').click()
        expect(admin.locator('#site-saved')).to_be_visible()

        for record, public_name, order in ((first, '供应商 A · 精选模型', '2'), (second, '供应商 B · 精选模型', '1')):
            admin.locator(f'[data-action="select"][data-id="{record["id"]}"]').click()
            expect(admin.locator('#target-model')).to_have_text(record['model'])
            admin.locator('#settings-open').click()
            admin.locator('#config-published').check()
            admin.locator('#config-public-name').fill(public_name)
            admin.locator('#config-public-order').fill(order)
            admin.locator('#save').click()
            expect(admin.locator('#settings-dialog')).not_to_be_visible()
        monitors.update(first['id'], {'enabled':True})
        monitors.tick(first['id'])
        complete(backend.claim())
        monitors.tick(first['id'])
        guest.locator('#public-refresh').click()
        expect(guest.locator('#home-title')).to_have_text('公开模型身份观测站')
        expect(guest.locator('#monitor-list .monitor-tile')).to_have_count(2)
        expect(guest.locator('#monitor-list .monitor-tile').first).to_contain_text('供应商 B')
        assert guest.locator('#home-announcement img').count() == 0
        assert guest.evaluate('window.injected') is None
        guest.locator(f'[data-monitor="{first["public_id"]}"]').click()
        expect(guest.locator('#featured-detail')).to_contain_text('完整结果')
        assert 'private-provider' not in guest.locator('body').inner_text()
        assert 'private administrator label' not in guest.locator('body').inner_text()
        guest.locator('#featured-detail [data-history-index]').click()
        expect(guest.locator('#public-detail-dialog')).to_be_visible()
        guest.locator('#public-detail-close').click()

        guest.locator('#test-url').fill('https://guest-provider.example/v1')
        guest.locator('#test-key').fill('guest-secret-key')
        guest.locator('#test-model').fill('<img src=x onerror="window.injected = true"> guest-model')
        guest.locator('#test-submit').click()
        expect(guest.locator('#public-detail-dialog')).to_be_visible()
        expect(guest.locator('#test-key')).to_have_value('')
        guest.locator('#public-detail-close').click()
        expect(guest.locator('#public-history tr[data-public-id]')).to_have_count(1)
        public_id = guest.locator('#public-history tr[data-public-id]').get_attribute('data-public-id')
        complete(backend.claim())
        guest.locator('#public-refresh').click()
        expect(guest.locator('#public-history')).to_contain_text('完整结果')
        assert guest.locator('#public-history img').count() == 0
        guest.locator('#public-history tr[data-public-id]').focus()
        guest.keyboard.press('Enter')
        expect(guest.locator('#public-detail-content')).to_contain_text('完整结果')
        # A delayed automatic poll must not replace a newly opened monitor history detail.
        delayed['id'] = public_id
        guest.evaluate("document.getElementById('public-refresh').click()")
        assert delay_started.wait(3)
        guest.keyboard.press('Escape')
        guest.locator('#featured-detail [data-history-index]').click()
        expect(guest.locator('#public-detail-content')).to_contain_text('admin-model')
        delay_release.set()
        guest.wait_for_event('response', predicate=lambda response: response.url.endswith('/public/api/detections/' + public_id), timeout=6000)
        expect(guest.locator('#public-detail-content')).to_contain_text('admin-model')
        guest.locator('#public-detail-close').click()
        assert guest.evaluate('window.injected') is None
        outsider = guest_context.new_page()
        outsider.goto(base + '/status')
        expect(outsider.locator('#public-history tr[data-public-id]')).to_have_count(1)
        assert outsider.request.get(base + '/status/api/monitors/' + first['id']).status == 401
        admin.locator('#moderation-refresh').click()
        expect(admin.locator(f'[data-moderate="{public_id}"]')).to_have_text('隐藏记录')
        admin.locator(f'[data-moderate="{public_id}"]').click()
        expect(admin.locator(f'[data-moderate="{public_id}"]')).to_have_text('恢复公开')
        guest.locator('#public-refresh').click()
        expect(guest.locator('#public-history tr[data-public-id]')).to_have_count(0)
        assert outsider.request.get(base + '/public/api/detections/' + public_id).status == 404
        admin.locator(f'[data-moderate="{public_id}"]').click()
        expect(admin.locator(f'[data-moderate="{public_id}"]')).to_have_text('隐藏记录')
        guest.locator('#public-refresh').click()
        expect(guest.locator('#public-history tr[data-public-id]')).to_have_count(1)
        admin.locator('#site-public_enabled').uncheck()
        admin.locator('#site-save').click()
        expect(admin.locator('#site-saved')).to_be_visible()
        guest.locator('#public-refresh').click()
        expect(guest.locator('#test-submit')).to_be_disabled()
        expect(guest.locator('#test-error')).to_contain_text('暂未开放')
        admin.locator('#site-public_enabled').check()
        admin.locator('#site-save').click()
        expect(admin.locator('#site-saved')).to_be_visible()
        guest.locator('#public-refresh').click()
        expect(guest.locator('#test-submit')).to_be_enabled()

        for number in range(21):
            task = backend.submit({'base_url':'https://guest-provider.example/v1/responses', 'api_key':'fixture-secret', 'model':f'公开模型 {number + 1}',
                                   'reasoning_effort':'low', 'public_access':True, 'public_id':uuid.uuid4().hex}, client_ip=f'192.0.2.{number+1}')
            backend.finish(task['id'], 'failed', 'upstream_error')
        guest.locator('#public-refresh').click()
        expect(guest.locator('#public-total')).to_have_text('22')
        expect(guest.locator('#public-history tr[data-public-id]')).to_have_count(20)
        guest.locator('#public-next').click()
        expect(guest.locator('#public-history tr[data-public-id]')).to_have_count(2)
        expect(guest.locator('#public-page')).to_have_text('第 2 / 2 页')
        guest.locator('#public-prev').click()
        expect(guest.locator('#public-page')).to_have_text('第 1 / 2 页')
        # Restore readable fixture text after checking untrusted HTML rendering.
        admin.locator('#site-announcement').fill('管理员精选监测持续更新。欢迎使用自己的连接发起单次检测。')
        admin.locator('#site-save').click()
        expect(admin.locator('#site-saved')).to_be_visible()
        guest.locator('#test-model').fill('gpt-6.1-sol')
        guest.locator('#test-url').fill('')
        guest.locator('#public-refresh').click()
        expect(guest.locator('#home-announcement')).to_contain_text('欢迎使用')
        guest.evaluate('window.scrollTo(0,0)')
        guest.screenshot(path=str(output / 'public-desktop.png'), full_page=True)
        guest.screenshot(path=str(output / 'public-desktop-top.png'))
        for width in (320,390,768,1024,1440):
            guest.set_viewport_size({'width':width,'height':1000})
            assert guest.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            expect(guest.get_by_role('link', name='发起检测 ↗', exact=True)).to_be_visible()
        guest.set_viewport_size({'width':390,'height':844})
        guest.screenshot(path=str(output / 'public-mobile.png'), full_page=True)
        admin.evaluate('window.scrollTo(0,0)')
        admin.screenshot(path=str(output / 'admin-desktop.png'), full_page=True)
        admin.set_viewport_size({'width':390,'height':844})
        assert admin.evaluate('document.documentElement.scrollWidth <= innerWidth')
        admin.screenshot(path=str(output / 'admin-mobile.png'), full_page=True)
        admin.locator('#site-title').fill('long-title' * 8)
        admin.locator('#site-save').click()
        expect(admin.locator('#site-saved')).to_be_visible()
        guest.locator('#public-refresh').click()
        expect(guest.locator('#home-title')).to_have_text('long-title' * 8)
        for width in (320,390,1024):
            guest.set_viewport_size({'width':width,'height':1000})
            assert guest.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        admin.locator('#admin-logout').click()
        expect(admin.locator('#login-form')).to_be_visible()
        assert admin_context.request.get(base + '/status/api/dashboards/default').status == 401
        if proxy_https:
            assert proxy_requests
            assert all(item['site'] == 'same-origin' and item['origin'] == base and not item['secure']
                       and item['host'] == 'internal.test:10001' for item in proxy_requests)
            assert any(item['path'] == '/admin/api/login' and item['status'] == 200 for item in proxy_requests)
            assert any(item['path'] == '/admin/api/site' and item['status'] == 200 for item in proxy_requests)
            assert any(item['path'] == '/admin/api/logout' and item['status'] == 200 for item in proxy_requests)
        assert not errors, errors
        browser.close()
    print('Public browser checks passed: login/logout, CSRF-backed admin settings, publication/order, anonymous submission, public results, moderation, feature switch, pagination, XSS escaping, and 320–1440px layout.')
    if proxy_https:
        print('HTTPS proxy checks passed with rewritten internal Host/HTTP, no trusted forwarding headers, and a Secure administrator cookie.')
    print(f'Screenshots: {output}')
finally:
    server.shutdown()
    server.server_close()
    thread.join()
