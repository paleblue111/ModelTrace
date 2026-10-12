"""Optional real-browser integration check. Uses fake Redis and numeric model fixtures."""
import os
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fakeredis
from cryptography.fernet import Fernet
from flask import request
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

os.environ['CREDENTIAL_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['MODELTRACE_API_TOKEN'] = 'browser-test-admin'
os.environ['ASYNC_ONLY'] = 'true'
os.environ['REDIS_URL'] = 'redis://test-only'

from app import app
from detection_store import DetectionStore
from detection_worker import run_task
from monitor_store import MonitorStore

backend = DetectionStore(fakeredis.FakeRedis(decode_responses=True))
app.extensions['detection_store'] = backend
monitors = MonitorStore(backend)
delayed = {'id': None}
delay_started, delay_release = threading.Event(), threading.Event()


@app.after_request
def delay_one_monitor_response(response):
    if delayed['id'] and request.path == '/status/api/monitors/' + delayed['id']:
        delayed['id'] = None
        delay_started.set()
        delay_release.wait(5)
    return response


# Seed real browser HTTP caches with incompatible assets at the old fixed URLs.
cache_seed = {'active': False}

@app.before_request
def serve_previous_assets():
    if cache_seed['active'] and request.path in ('/static/status.js', '/static/status.css'):
        is_js = request.path.endswith('.js')
        return ('window.previousAssetLoaded = true;' if is_js else 'body { --previous-asset: yes; }',
                200, {'Content-Type': 'text/javascript' if is_js else 'text/css',
                      'Cache-Control': 'public, max-age=14400'})

@app.get('/test/cache-seed')
def seed_page():
    return '<link rel="stylesheet" href="/static/status.css"><script src="/static/status.js"></script>'

server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f'http://127.0.0.1:{server.server_port}'
output = Path(os.getenv('STATUS_SCREENSHOT_DIR', '/tmp/modeltrace-status-preview'))
output.mkdir(parents=True, exist_ok=True)

def heartbeat():
    backend.redis.set('modeltrace:scheduler:heartbeat', time.time(), ex=15)

def complete(monitor_id, valid=3):
    monitors.tick(monitor_id)
    task = backend.claim()
    assert task
    calls = []
    def probe(*args):
        calls.append(1)
        text = ','.join(str((index*73)%355+1) for index in range(330)) if len(calls) <= valid else 'invalid'
        return {'ok':True,'text':text}
    run_task(backend, task, probe)
    monitors.tick(monitor_id)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(extra_http_headers={'Authorization':'Bearer browser-test-admin'}, viewport={'width':1440,'height':1100}, device_scale_factor=1)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/admin')
        expect(page.locator('h1')).to_contain_text('每一次调用')
        page.screenshot(path=str(output / 'empty-desktop.png'), full_page=True)
        page.locator('#monitor-add').click()
        page.locator('#config-name').fill('供应商 A · 主力模型')
        page.locator('#config-url').fill('https://example.com/v1')
        page.locator('#config-key').fill('test-upstream-key-do-not-store-in-browser')
        page.locator('#config-model').fill('gpt-6-sol')
        page.locator('#config-effort').select_option('high')
        page.locator('#config-interval').fill('15')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        monitor_id = monitors.default()['id']
        assert page.url == base + '/admin#monitor=' + monitor_id
        assert 'test-upstream-key' not in page.evaluate('JSON.stringify(localStorage)')
        assert not monitors.get(monitor_id)['enabled']
        heartbeat()
        page.locator('#run').click()
        expect(page.locator('#active-panel')).to_be_visible()
        assert monitors.get(monitor_id)['run_requested']
        complete(monitor_id)
        page.locator('#refresh').click()
        expect(page.locator('#result-badge')).to_have_text('完整结果')
        expect(page.locator('#history tr[data-id]')).to_have_count(1)
        assert len(page.locator('#candidates .candidate').all()) == 5
        page.screenshot(path=str(output / 'status-desktop.png'), full_page=True)
        # An independent browser ignores its old local IDs and loads the shared panel.
        other_context = browser.new_context(extra_http_headers={'Authorization':'Bearer browser-test-admin'})
        other_context.add_init_script('localStorage.setItem("modeltrace-monitor", "ffffffffffffffffffffffffffffffff"); localStorage.setItem("modeltrace-dashboard", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")')
        other_page = other_context.new_page()
        other_page.goto(base + '/admin')
        expect(other_page.locator('#target-model')).to_have_text('gpt-6-sol')
        expect(other_page.locator('#history tr[data-id]')).to_have_count(1)
        expect(other_page.locator('#result-badge')).to_have_text('完整结果')
        assert other_page.url == base + '/admin#monitor=' + monitor_id
        other_context.close()
        page.locator('#history tr[data-id]').click()
        expect(page.locator('#history-dialog')).to_be_visible()
        page.locator('[data-close="history-dialog"]').click()
        page.locator('#settings-open').click()
        expect(page.locator('#config-key')).to_have_value('')
        page.locator('#config-url').fill('https://changed.example/v1')
        expect(page.locator('#config-key')).to_have_attribute('required', '')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).to_be_visible()
        page.locator('#config-url').fill('https://example.com/v1')
        assert not page.locator('#config-key').evaluate('(input) => input.required')
        page.locator('#config-model').fill('gpt-6-sol-updated')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        assert backend.cipher.decrypt(monitors.get(monitor_id)['encrypted_key'].encode()).decode() == 'test-upstream-key-do-not-store-in-browser'
        page.locator('#toggle').click()
        expect(page.locator('#monitor-badge')).to_have_text('持续监测')
        page.close()
        # Browser is closed. The server still creates and completes the next scheduled detection.
        complete(monitor_id, valid=2)
        heartbeat()
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/admin')
        expect(page.locator('#result-badge')).to_have_text('样本不足')
        expect(page.locator('#history tr[data-id]')).to_have_count(2)
        page.locator('#toggle').click()
        expect(page.locator('#monitor-badge')).to_have_text('已暂停')
        assert not monitors.get(monitor_id)['enabled']
        # Reuse the first provider's credentials for another model without reading its key.
        page.locator('#monitor-copy').click()
        expect(page.locator('#config-key')).to_have_value('')
        page.locator('#config-name').fill('供应商 A · 备用模型')
        page.locator('#config-model').fill('gpt-6-astra')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        second_id = page.locator('.monitor-tile.selected').get_attribute('data-monitor-id')
        assert second_id != monitor_id
        assert backend.cipher.decrypt(monitors.get(second_id)['encrypted_key'].encode()).decode() == 'test-upstream-key-do-not-store-in-browser'
        expect(page.locator('#history tr[data-id]')).to_have_count(0)
        page.locator('#toggle').click()
        expect(page.locator('#monitor-badge')).to_have_text('持续监测')

        # A different provider keeps its own URL, key, model and interval.
        page.locator('#monitor-filter').fill('no-existing-match')
        page.locator('#monitor-add').click()
        expect(page.locator('#config-url')).to_have_value('')
        page.locator('#config-name').fill('供应商 B · 对比模型')
        page.locator('#config-url').fill('https://another.example/v1')
        page.locator('#config-key').fill('second-provider-secret')
        page.locator('#config-model').fill('gpt-6.1-sol')
        page.locator('#config-effort').select_option('low')
        page.locator('#config-interval').fill('60')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        third_id = page.locator('.monitor-tile.selected').get_attribute('data-monitor-id')
        expect(page.locator('.monitor-tile')).to_have_count(3)
        page.locator('#toggle').click()
        expect(page.locator('#monitor-badge')).to_have_text('持续监测')
        assert not monitors.get(monitor_id)['enabled']
        assert monitors.get(second_id)['enabled'] and monitors.get(third_id)['enabled']
        assert monitors.get(second_id)['interval_seconds'] == 900
        assert monitors.get(third_id)['interval_seconds'] == 3600
        assert page.url == base + '/admin#monitor=' + third_id
        board_id = monitors.default_dashboard()['id']
        saved_url = base + f'/admin#dashboard={board_id}&monitor={third_id}'
        assert set(monitors.get_dashboard(board_id)['monitor_ids']) == {monitor_id, second_id, third_id}
        assert not any(secret in page.evaluate('JSON.stringify(localStorage)') for secret in ('test-upstream-key', 'second-provider-secret'))
        page.close()
        complete(second_id)
        complete(third_id, valid=2)
        heartbeat()

        # A fresh browser restores all items from the server using one bookmark.
        fresh_context = browser.new_context(extra_http_headers={'Authorization':'Bearer browser-test-admin'}, viewport={'width':1440,'height':1100})
        page = fresh_context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/admin')
        expect(page.locator('.monitor-tile')).to_have_count(3)
        expect(page.locator('#target-model')).to_have_text('gpt-6-sol-updated')
        # Explicit bookmarks still select the requested item.
        page.goto(saved_url)
        expect(page.locator('.monitor-tile')).to_have_count(3)
        expect(page.locator('#target-model')).to_have_text('gpt-6.1-sol')
        expect(page.locator('#result-badge')).to_have_text('样本不足')
        page.locator(f'[data-action="select"][data-id="{monitor_id}"]').click()
        expect(page.locator('#target-model')).to_have_text('gpt-6-sol-updated')
        expect(page.locator('#history tr[data-id]')).to_have_count(2)
        # A delayed poll for the old selection must not replace the newly selected item.
        delayed['id'] = monitor_id
        page.locator('#refresh').click()
        assert delay_started.wait(3)
        page.locator(f'[data-action="select"][data-id="{second_id}"]').click()
        delay_release.set()
        expect(page.locator('#target-model')).to_have_text('gpt-6-astra')
        expect(page.locator('#history tr[data-id]')).to_have_count(1)
        expect(page.locator('#result-badge')).to_have_text('完整结果')
        page.locator(f'[data-action="toggle"][data-id="{second_id}"]').click()
        expect(page.locator('#monitor-badge')).to_have_text('已暂停')
        assert not monitors.get(second_id)['enabled'] and monitors.get(third_id)['enabled']
        page.locator('#monitor-filter').fill('供应商 B')
        expect(page.locator('.monitor-tile')).to_have_count(1)
        page.locator('#monitor-filter').fill('')
        expect(page.locator('.monitor-tile')).to_have_count(3)
        # Desktop monitoring cards live entirely inside the left sidebar.
        expect(page.locator('.sidebar #monitor-list .monitor-tile')).to_have_count(3)
        sidebar = page.locator('.sidebar').bounding_box()
        selected_tile = page.locator('.monitor-tile.selected').bounding_box()
        details = page.locator('#monitor-details').bounding_box()
        assert selected_tile['x'] + selected_tile['width'] <= sidebar['x'] + sidebar['width']
        assert details['x'] >= sidebar['x'] + sidebar['width']
        page.screenshot(path=str(output / 'status-desktop.png'), full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output / 'status-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#settings-open').click()
        page.screenshot(path=str(output / 'settings-mobile.png'), full_page=True)
        page.get_by_role('button', name='关闭设置', exact=True).click()

        # Legacy bookmarks are adopted without recreating the original monitor.
        legacy_context = browser.new_context(extra_http_headers={'Authorization':'Bearer browser-test-admin'})
        legacy_page = legacy_context.new_page()
        legacy_page.on('pageerror', lambda error: errors.append(str(error)))
        before = monitors.get(monitor_id)
        legacy_page.goto(base + '/admin#monitor=' + monitor_id)
        expect(legacy_page.locator('#target-model')).to_have_text(before['model'])
        expect(legacy_page.locator('#history tr[data-id]')).to_have_count(2)
        assert monitors.get(monitor_id) == before
        expect(legacy_page.locator('.monitor-tile')).to_have_count(3)
        legacy_page.goto(base + '/admin#monitor=' + monitor_id)
        legacy_page.reload()
        expect(legacy_page.locator('.monitor-tile')).to_have_count(3)
        assert len(list(backend.redis.scan_iter('modeltrace:dashboard:*'))) == 1
        # Remember the selected monitor through reloads on the default shared panel.
        legacy_page.locator(f'[data-action="select"][data-id="{third_id}"]').click()
        expect(legacy_page.locator('#target-model')).to_have_text('gpt-6.1-sol')
        legacy_page.reload()
        expect(legacy_page.locator('#target-model')).to_have_text('gpt-6.1-sol')
        # Failed initialization must remain retryable, including legacy migration POSTs.
        for suffix, failed_endpoint in [('/admin', '**/status/api/dashboards/default'),
                                        ('/admin#monitor=' + monitor_id, '**/status/api/dashboards')]:
            recovery = fresh_context.new_page()
            recovery.on('pageerror', lambda error: errors.append(str(error)))
            recovery.route(failed_endpoint, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"temporary outage"}'))
            recovery.goto(base + suffix)
            expect(recovery.locator('#notice')).to_contain_text('temporary outage')
            expect(recovery.locator('#monitor-add')).to_be_disabled()
            expect(recovery.locator('#refresh')).to_be_enabled()
            recovery.unroute(failed_endpoint)
            recovery.locator('#refresh').click()
            expect(recovery.locator('.monitor-tile')).to_have_count(3)
            expect(recovery.locator('#monitor-add')).to_be_enabled()
            recovery.close()
        # A delayed save must lock the form and must not overwrite a newly opened panel.
        editing_page = fresh_context.new_page()
        editing_page.on('pageerror', lambda error: errors.append(str(error)))
        editing_page.goto(base + '/admin#monitor=' + monitor_id)
        expect(editing_page.locator('#target-model')).to_have_text(before['model'])
        editing_page.locator('#settings-open').click()
        editing_page.locator('#config-name').fill('保存中的名称')
        delay_started.clear()
        delay_release.clear()
        delayed['id'] = monitor_id
        editing_page.locator('#save').click()
        assert delay_started.wait(3)
        expect(editing_page.locator('#config-name')).to_be_disabled()
        editing_page.keyboard.press('Escape')
        expect(editing_page.locator('#settings-dialog')).to_be_visible()
        isolated = monitors.create_dashboard()
        isolated_url = base + '/admin#dashboard=' + isolated['id']
        editing_page.evaluate('(hash) => { location.hash = hash; }', '#dashboard=' + isolated['id'])
        expect(editing_page.locator('#settings-dialog')).not_to_be_visible()
        delay_release.set()
        expect(editing_page.locator('#monitor-add')).to_be_enabled()
        expect(editing_page.locator('.monitor-tile')).to_have_count(0)
        assert editing_page.url == isolated_url
        assert monitors.get(monitor_id)['name'] == '保存中的名称'
        editing_page.close()
        # Previously cached unversioned assets must never execute on the new page.
        cached_context = browser.new_context(extra_http_headers={'Authorization':'Bearer browser-test-admin'})
        cached_page = cached_context.new_page()
        cached_page.on('pageerror', lambda error: errors.append(str(error)))
        cache_seed['active'] = True
        cached_page.goto(base + '/test/cache-seed', wait_until='networkidle')
        assert cached_page.evaluate('window.previousAssetLoaded') is True
        cache_seed['active'] = False
        asset_requests = []
        cached_page.on('request', lambda req: asset_requests.append(req.url))
        cached_page.goto(base + '/admin')
        expect(cached_page.locator('#monitor-add')).to_be_enabled()
        assert cached_page.evaluate('window.previousAssetLoaded') is None
        assert not any('/static/status.' in url for url in asset_requests)
        assert sum('/status/assets/' in url for url in asset_requests) == 5
        cached_page.locator('#monitor-add').click()
        expect(cached_page.locator('#settings-dialog')).to_be_visible()
        cached_context.close()

        # One failed detail endpoint must not hide the list or block other monitors.
        partial = fresh_context.new_page()
        partial.on('pageerror', lambda error: errors.append(str(error)))
        broken_endpoint = '**/status/api/monitors/' + monitor_id
        partial.route(broken_endpoint, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"detail unavailable"}'))
        partial.goto(base + '/admin')
        expect(partial.locator('#notice')).to_contain_text('详情加载失败')
        expect(partial.locator('.monitor-tile')).to_have_count(3)
        expect(partial.locator('#monitor-add')).to_be_enabled()
        partial.locator('#monitor-add').click()
        expect(partial.locator('#settings-dialog')).to_be_visible()
        partial.get_by_role('button', name='关闭设置', exact=True).click()
        partial.locator(f'[data-action="select"][data-id="{second_id}"]').click()
        expect(partial.locator('#target-model')).to_have_text('gpt-6-astra')
        partial.unroute(broken_endpoint)
        partial.locator(f'[data-action="select"][data-id="{monitor_id}"]').click()
        expect(partial.locator('#target-model')).to_have_text(before['model'])
        expect(partial.locator('#notice')).not_to_contain_text('详情加载失败')

        # Keep the focused history record through two automatic polling cycles.
        row = partial.locator('#history tr[data-id]').first
        focused_id = row.get_attribute('data-id')
        row.focus()
        for _ in range(2):
            partial.wait_for_event('response', predicate=lambda response: response.url.endswith('/status/api/monitors/' + monitor_id), timeout=6000)
            expect(partial.locator(f'#history tr[data-id="{focused_id}"]')).to_be_focused()
        partial.keyboard.press('Enter')
        expect(partial.locator('#history-dialog')).to_be_visible()
        partial.get_by_role('button', name='关闭详情', exact=True).click()

        # Realistic and maximum-duration values must stay on one line at every width.
        for elapsed, text in [(69, '1 分 9 秒'), (599, '9 分 59 秒'), (1799, '29 分 59 秒')]:
            record = monitors.get(monitor_id)
            record['history'][0]['elapsed_seconds'] = elapsed
            backend.redis.set(monitors.key(monitor_id), json.dumps(record))
            partial.locator('#refresh').click()
            expect(partial.locator('#duration')).to_have_text(text)
            for width in (320, 390, 620, 768, 1024, 1440):
                partial.set_viewport_size({'width': width, 'height': 1000})
                metrics = partial.locator('#duration').evaluate("""el => {
                    const range = document.createRange(); range.selectNodeContents(el);
                    const lines = [...range.getClientRects()];
                    const parent = el.parentElement.getBoundingClientRect();
                    return {lines: lines.length, fits: lines.every(line => line.right <= parent.right + 1),
                            overflow: document.documentElement.scrollWidth > innerWidth};
                }""")
                assert metrics == {'lines': 1, 'fits': True, 'overflow': False}, (width, text, metrics)
            if elapsed == 69:
                partial.screenshot(path=str(output / 'fixed-desktop.png'), full_page=True)
                partial.set_viewport_size({'width':390,'height':844})
                partial.screenshot(path=str(output / 'fixed-mobile.png'), full_page=True)
        partial.close()

        # Long model/provider names must not stretch tablet or mobile grids.
        monitors.update(monitor_id, {'name': '供应商' * 26, 'model': 'long-model-' * 50})
        legacy_page.goto(base + '/admin#monitor=' + monitor_id)
        expect(legacy_page.locator('#target-model')).to_have_text('long-model-' * 50)
        for width in (320, 390, 768, 1024, 1440):
            legacy_page.set_viewport_size({'width': width, 'height': 1000})
            assert legacy_page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width

        # Deleting from the sidebar is cancellable and recovers from API errors.
        deletion = fresh_context.new_page()
        deletion.on('pageerror', lambda error: errors.append(str(error)))
        deletion.goto(base + '/admin#monitor=' + second_id)
        expect(deletion.locator('#target-model')).to_have_text('gpt-6-astra')
        observe = fresh_context.new_page()
        observe.on('pageerror', lambda error: errors.append(str(error)))
        observe.goto(base + '/admin#monitor=' + monitor_id)
        expect(observe.locator('#target-model')).to_have_text('long-model-' * 50)
        remove_first = deletion.locator(f'[data-action="delete"][data-id="{monitor_id}"]')
        remove_first.click()
        expect(deletion.locator('#delete-dialog')).to_be_visible()
        expect(deletion.locator('#delete-name')).to_have_text('供应商' * 26)
        deletion.locator('#delete-dialog [data-close="delete-dialog"]').last.click()
        expect(deletion.locator('.monitor-tile')).to_have_count(3)
        assert monitors.get(monitor_id)
        failed_delete = '**/status/api/monitors/' + monitor_id
        deletion.route(failed_delete, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"delete unavailable"}') if route.request.method == 'DELETE' else route.continue_())
        remove_first.click()
        deletion.locator('#delete-confirm').click()
        expect(deletion.locator('#delete-error')).to_have_text('delete unavailable')
        expect(deletion.locator('#delete-confirm')).to_be_enabled()
        expect(deletion.locator('.monitor-tile')).to_have_count(3)
        assert monitors.get(monitor_id)
        deletion.unroute(failed_delete)
        deletion.locator('#delete-confirm').click()
        expect(deletion.locator('#delete-dialog')).not_to_be_visible()
        expect(deletion.locator('.monitor-tile')).to_have_count(2)
        expect(deletion.locator('#target-model')).to_have_text('gpt-6-astra')
        assert deletion.url == base + '/admin#monitor=' + second_id
        assert monitors.get(monitor_id) is None
        observe.locator('#refresh').click()
        expect(observe.locator('.monitor-tile')).to_have_count(2)
        expect(observe.locator('#target-model')).to_have_text('gpt-6-astra')
        observe.goto(base + '/admin#monitor=' + monitor_id)
        expect(observe.locator('#monitor-add')).to_be_enabled()
        expect(observe.locator('#target-model')).to_have_text('gpt-6-astra')

        # Removing the selected running monitor cancels its task and selects a neighbor.
        deletion.locator('#run').click()
        expect(deletion.locator('#active-panel')).to_be_visible()
        monitors.tick(second_id)
        removed_task = backend.claim()
        assert removed_task
        deletion.locator(f'[data-action="delete"][data-id="{second_id}"]').click()
        expect(deletion.locator('#delete-active-note')).to_be_visible()
        deletion.locator('#delete-confirm').click()
        expect(deletion.locator('.monitor-tile')).to_have_count(1)
        expect(deletion.locator('#target-model')).to_have_text('gpt-6.1-sol')
        assert backend.get(removed_task['id'])['status'] == 'cancelled'
        assert monitors.get(third_id)['enabled']
        assert deletion.url == base + '/admin#monitor=' + third_id

        # Last-item removal leaves a usable empty panel and survives reloads.
        deletion.set_viewport_size({'width':390,'height':844})
        deletion.locator(f'[data-action="delete"][data-id="{third_id}"]').click()
        deletion.screenshot(path=str(output / 'delete-mobile.png'), full_page=True)
        assert deletion.evaluate('document.documentElement.scrollWidth <= innerWidth')
        deletion.locator('#delete-confirm').click()
        expect(deletion.locator('.monitor-tile')).to_have_count(0)
        expect(deletion.locator('#monitor-details')).not_to_be_visible()
        expect(deletion.locator('#monitor-empty')).to_be_visible()
        expect(deletion.locator('#monitor-count')).to_have_text('0')
        assert deletion.url == base + '/admin'
        deletion.reload()
        expect(deletion.locator('#monitor-empty')).to_be_visible()
        expect(deletion.locator('#monitor-add')).to_be_enabled()
        deletion.locator('#monitor-add').click()
        deletion.locator('#config-name').fill('删除后重新添加')
        deletion.locator('#config-url').fill('https://replacement.example/v1')
        deletion.locator('#config-key').fill('replacement-secret')
        deletion.locator('#config-model').fill('replacement-model')
        deletion.locator('#save').click()
        expect(deletion.locator('#settings-dialog')).not_to_be_visible()
        expect(deletion.locator('#target-model')).to_have_text('replacement-model')
        expect(deletion.locator('.monitor-tile')).to_have_count(1)
        expect(deletion.locator('#monitor-empty')).not_to_be_visible()
        assert not errors, errors
        browser.close()
    print('Browser integration passed: shared dashboard across browsers, root redirect, multiple providers/models, key reuse, independent history/schedules, bookmark restore, legacy migration, search, and desktop/mobile layout.')
    print('Regression checks passed: cached old assets, isolated detail failures, keyboard focus through polling, single-line durations at 320–1440px.')
    print('Sidebar/deletion checks passed: left-side cards, cancel/retry, cross-browser removal, deleted bookmarks, running-task cancellation, neighbor selection, last-item removal, and adding again.')
    print(f'Screenshots: {output}')
finally:
    server.shutdown()
    server.server_close()
    thread.join()
