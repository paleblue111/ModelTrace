"""Optional real-browser integration check. Uses fake Redis and numeric model fixtures."""
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fakeredis
from cryptography.fernet import Fernet
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
        context = browser.new_context(viewport={'width':1440,'height':1100}, device_scale_factor=1)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/status')
        expect(page.locator('h1')).to_contain_text('每一次调用')
        page.screenshot(path=str(output / 'empty-desktop.png'), full_page=True)
        page.locator('#settings-open').click()
        page.locator('#config-url').fill('https://example.com/v1')
        page.locator('#config-key').fill('test-upstream-key-do-not-store-in-browser')
        page.locator('#config-model').fill('gpt-6-sol')
        page.locator('#config-effort').select_option('high')
        page.locator('#config-interval').fill('15')
        page.locator('#save').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        monitor_id = page.evaluate('localStorage.getItem("modeltrace-monitor")')
        assert monitor_id and '#monitor=' in page.url
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
        page.locator('#history tr[data-id]').click()
        expect(page.locator('#history-dialog')).to_be_visible()
        page.locator('[data-close="history-dialog"]').click()
        page.locator('#settings-open').click()
        expect(page.locator('#config-key')).to_have_value('')
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
        page.goto(base + '/status')
        expect(page.locator('#result-badge')).to_have_text('样本不足')
        expect(page.locator('#history tr[data-id]')).to_have_count(2)
        page.locator('#toggle').click()
        expect(page.locator('#monitor-badge')).to_have_text('已暂停')
        assert not monitors.get(monitor_id)['enabled']
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output / 'status-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#settings-open').click()
        page.screenshot(path=str(output / 'settings-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
    print('Browser integration passed: settings, polling, key retention, history, background scheduling, pause, mobile layout.')
    print(f'Screenshots: {output}')
finally:
    server.shutdown()
    server.server_close()
    thread.join()
