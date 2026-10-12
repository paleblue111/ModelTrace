const {adminFetch} = await import(document.body.dataset.adminClient);
const $ = (id) => document.getElementById(id);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'排队中',running:'检测中',completed:'完整结果',partial:'样本不足',failed:'检测失败',cancelled:'已取消',interrupted:'执行中断'};
const reasons = {target_reached:'已收集三份有效回答',attempts_exhausted:'已达到本轮尝试次数上限',upstream_error:'上游请求失败',task_timeout:'达到任务总时限',queue_timeout:'排队超时',user_cancelled:'用户取消',worker_lost:'执行进程中断',worker_shutdown:'执行进程关闭',worker_error:'执行异常'};
const pct = (value) => typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '—';
const duration = (value) => typeof value !== 'number' ? '—' : value < 60 ? `${Math.round(value)} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
const date = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const badge = (status) => ['completed'].includes(status) ? 'success' : ['partial','interrupted'].includes(status) ? 'warning' : status === 'failed' ? 'danger' : 'neutral';
const validId = (id) => typeof id === 'string' && /^[0-9a-f]{32}$/.test(id);
const title = (item) => item.name || item.model;
const pending = (item) => Boolean(item.active_task_id || item.run_requested);
const emptyHistory = $('history').innerHTML;
let dashboardId = null;
let monitorId = null;
let sharedDefault = true;
let monitors = [];
let schedulerOnline = false;
let model = null;
let busy = false;
let loading = false;
let ready = false;
let revision = 0;
let networkError = '';
let detailError = '';
let editingId = null;
let sourceId = null;
let sourceUrl = '';
let deletingId = null;
const normalizedUrl = (value) => {
  const url = value.trim().replace(/\/+$/, '');
  return url.endsWith('/responses') ? url : url + (url.endsWith('/v1') ? '/responses' : '/v1/responses');
};

async function request(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await adminFetch(url, {...options, headers:{'Content-Type':'application/json', ...options.headers}, signal:controller.signal});
    let data;
    try { data = await response.json(); } catch { throw new Error('服务器未返回有效数据，请检查服务是否启动'); }
    if (!response.ok) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('连接超时，请刷新确认服务器状态；不要重复提交操作');
    throw error;
  } finally { clearTimeout(timeout); }
}
function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}
function remember() {
  if (sharedDefault) {
    history.replaceState(null, '', `${location.pathname}${location.search}${monitorId ? '#monitor=' + monitorId : ''}`);
    return;
  }
  // Explicit capability links remain usable without relying on browser storage.
  const fragment = new URLSearchParams();
  if (dashboardId) fragment.set('dashboard', dashboardId);
  if (monitorId) fragment.set('monitor', monitorId);
  history.replaceState(null, '', `${location.pathname}${location.search}${fragment.size ? '#' + fragment : ''}`);
}
function upsert(saved) {
  const latest = saved.history[0];
  const summary = {...saved, latest:latest ? {...latest, prediction_name:latest.result?.prediction_name, probability:latest.result?.probability, valid_samples:latest.progress.valid} : null};
  const index = monitors.findIndex((item) => item.id === saved.id);
  if (index < 0) monitors.push(summary); else monitors[index] = summary;
  if (saved.id === monitorId) model = saved;
  schedulerOnline = saved.scheduler_online;
}
function renderList() {
  $('monitor-add').disabled = busy || !ready;
  $('monitor-count').textContent = monitors.length;
  $('monitor-summary').textContent = monitors.length ? `${monitors.filter((item) => item.enabled).length} 个已开启 · ${monitors.filter(pending).length} 个待完成` : '每个监测独立保存结果';
  const query = $('monitor-filter').value.trim().toLowerCase();
  const visible = monitors.filter((item) => `${item.name || ''} ${item.url} ${item.model}`.toLowerCase().includes(query));
  // Preserve keyboard focus when background polling updates list controls.
  const focused = document.activeElement?.closest('#monitor-list [data-action]');
  const focusKey = focused ? [focused.dataset.id, focused.dataset.action] : null;
  $('monitor-list').innerHTML = !ready ? '<div class="empty"><strong>正在连接监测面板</strong><p>连接失败时可点击刷新重试。</p></div>' : visible.length ? visible.map((item) => {
    const inProgress = pending(item);
    const status = inProgress ? '检测进行中' : item.enabled ? '持续监测' : '已暂停';
    const latest = item.latest;
    return `<article class="monitor-tile ${item.id === monitorId ? 'selected' : ''}" data-monitor-id="${escape(item.id)}">
      <button type="button" class="monitor-select" data-action="select" data-id="${escape(item.id)}" aria-pressed="${item.id === monitorId}">
        <span class="tile-heading"><strong>${escape(title(item))}</strong><span class="badge ${item.enabled ? 'success' : 'neutral'}">${status}</span></span>
        <span class="tile-provider" title="${escape(item.url)}">${escape(item.url)}</span>
        <span class="tile-model">${escape(item.model)} · ${escape(item.reasoning_effort)} · 每 ${item.interval_seconds/60} 分钟 · ${item.published ? '已公开' : '仅后台'}</span>
        <span class="tile-result">${latest ? `<b>${escape(latest.prediction_name || labels[latest.status] || '暂无归因')}</b> · ${pct(latest.probability)}<br>${escape(labels[latest.status] || '未知')} · 样本 ${latest.valid_samples}/3 · ${escape(date(latest.finished_at))}` : '等待第一轮检测<br>结果与历史独立保存'}</span>
      </button>
      <div class="tile-actions"><button type="button" class="button tile-delete" data-action="delete" data-id="${escape(item.id)}" aria-label="删除 ${escape(title(item))}" ${busy || !ready ? 'disabled' : ''}>删除</button><button type="button" class="button secondary" data-action="run" data-id="${escape(item.id)}" ${busy || inProgress || !ready ? 'disabled' : ''}>立即检测</button><button type="button" class="button ${item.enabled ? 'secondary' : 'primary'}" data-action="toggle" data-id="${escape(item.id)}" ${busy || !ready ? 'disabled' : ''}>${item.enabled ? '暂停监测' : '开启监测'}</button></div>
    </article>`;
  }).join('') : `<div class="empty"><strong>${monitors.length ? '没有找到匹配的监测' : '从添加第一个监测开始'}</strong><p>${monitors.length ? '试试其他名称、供应商地址或模型名。' : '点击「新增监测」，分别添加不同供应商和模型。'}</p></div>`;
  if (focusKey) $('monitor-list').querySelector(`[data-id="${focusKey[0]}"][data-action="${focusKey[1]}"]`)?.focus({preventScroll:true});
}
function renderCandidates(result, target = $('candidates')) {
  if (!result?.results?.length) {
    target.innerHTML = '<div class="empty"><span class="empty-symbol">≋</span><strong>还没有匹配结果</strong><p>运行一次检测，查看最接近的候选模型。</p></div>';
    return;
  }
  target.innerHTML = result.results.slice(0, 5).map((item, index) => `<div class="candidate"><span class="rank">${String(index+1).padStart(2,'0')}</span><div class="candidate-main"><div class="candidate-name">${escape(item.display_name)}</div><div class="bar"><i style="width:${Math.max(0,Math.min(100,Number(item.probability)*100))}%"></i></div></div><span class="candidate-score">${pct(item.probability)}</span></div>`).join('');
}
function countdown() {
  if (!model) return;
  if (model.active_task_id) $('next-run').textContent = model.enabled ? '当前检测结束后安排' : '已暂停';
  else if (model.run_requested) $('next-run').textContent = '等待调度';
  else if (model.enabled && model.next_run_at) {
    const left = Math.ceil(model.next_run_at - Date.now()/1000);
    $('next-run').textContent = left <= 0 ? '等待调度' : `${Math.floor(left/60)} 分 ${left%60} 秒后`;
  } else $('next-run').textContent = '已暂停';
}
function render() {
  renderList();
  $('monitor-empty').hidden = !ready || monitors.length > 0;
  $('monitor-details').hidden = !model;
  $('connection').textContent = networkError ? '连接暂时中断' : !dashboardId ? '等待配置' : schedulerOnline ? '调度服务在线' : '调度服务离线';
  $('connection').className = `connection ${networkError ? 'error' : schedulerOnline ? 'online' : ''}`;
  $('refresh').disabled = loading || busy;
  if (!model) { notice(networkError || detailError || (monitorId && ready ? '正在加载所选监测…' : '')); return; }
  $('selected-name').textContent = title(model);
  $('settings-open').disabled = busy || !ready;
  $('monitor-copy').disabled = busy || !ready;
  const active = model.active_task;
  const latest = model.history[0];
  const result = latest?.result;
  const pending = Boolean(model.active_task_id || model.run_requested);
  $('monitor-badge').textContent = model.enabled ? '持续监测' : '已暂停';
  $('monitor-badge').className = `badge ${model.enabled ? 'success' : 'neutral'}`;
  $('monitor-title').textContent = pending ? '正在收集模型指纹' : model.enabled ? '监测已开启' : '随时开始下一次检测';
  $('monitor-detail').textContent = pending ? '每轮独立采样，结果会在完成后自动更新。' : model.enabled ? '后台按计划执行，你可以放心关闭这个页面。' : '配置已保存在服务器，可立即检测或开启定时监测。';
  $('target-model').textContent = model.model;
  $('target-effort').textContent = model.reasoning_effort;
  $('toggle').textContent = model.enabled ? '暂停定时监测' : '开启定时监测';
  $('toggle').disabled = busy || !ready;
  $('run').disabled = busy || pending || !ready;
  $('run').textContent = pending ? '检测进行中' : '立即检测';
  $('schedule').textContent = model.enabled ? `每轮结束 ${model.interval_seconds/60} 分钟后自动检测 · 服务器执行` : '定时监测已暂停，当前任务仍会完成';
  $('endpoint').textContent = model.url;
  $('interval').textContent = `${model.interval_seconds/60} 分钟`;
  $('total-runs').textContent = `${model.total_runs} 次`;
  $('last-finished').textContent = date(latest?.finished_at);
  $('result-badge').textContent = latest ? labels[latest.status] : '等待检测';
  $('result-badge').className = `badge ${badge(latest?.status)}`;
  $('prediction').textContent = result?.prediction_name || (latest ? '本轮未获得归因结果' : '等待第一份指纹');
  $('result-note').textContent = result ? (result.sample_warning || `${date(latest.finished_at)} · ${result.family_prediction_name || '模型'}家族`) : latest ? (reasons[latest.reason] || '查看检测详情了解原因') : '完成检测后，模型归因结果将显示在这里。';
  $('probability').textContent = pct(result?.probability);
  $('samples').innerHTML = `${latest ? latest.progress.valid : '—'}<small> / 3</small>`;
  $('duration').textContent = duration(latest?.elapsed_seconds);
  $('active-panel').hidden = !pending;
  $('active-title').textContent = active ? labels[active.status] : '等待服务器调度';
  $('active-detail').textContent = active ? `有效 ${active.progress.valid}/3 · 已尝试 ${active.progress.attempted}/6 · ${duration(active.elapsed_seconds)}` : '即将提交一轮检测';
  $('active-progress').value = active?.progress.valid || 0;
  renderCandidates(result);
  $('history-count').textContent = model.history.length;
  const focusedHistoryId = document.activeElement?.closest('#history tr[data-id]')?.dataset.id;
  if (model.history.length) {
    $('history').innerHTML = model.history.map((item) => `<tr data-id="${escape(item.id)}" tabindex="0" role="button" aria-label="查看 ${escape(date(item.finished_at))} 的检测详情"><td>${escape(date(item.finished_at))}</td><td>${escape(item.requested_model)}</td><td>${escape(item.result?.prediction_name || '—')}</td><td>${pct(item.result?.probability)}</td><td>${item.progress.valid} / 3</td><td><span class="badge ${badge(item.status)}">${labels[item.status] || '未知'}</span></td><td>${duration(item.elapsed_seconds)}</td></tr>`).join('');
  } else $('history').innerHTML = emptyHistory;
  if (focusedHistoryId) {
    const row = [...$('history').querySelectorAll('tr[data-id]')].find((item) => item.dataset.id === focusedHistoryId);
    (row || $('refresh')).focus({preventScroll:true});
  }
  const warnings = [networkError, model.last_error];
  if (!schedulerOnline) warnings.push('调度服务暂未在线，检查 Docker 中的 scheduler 服务。页面不会替代服务器发起定时检测。');
  notice(warnings.filter(Boolean).join('；'));
  countdown();
}
async function refresh() {
  if (loading || busy) return;
  loading = true;
  $('refresh').disabled = true;
  const expectedRevision = revision;
  const requestedDashboard = sharedDefault ? 'default' : dashboardId;
  try {
    let dashboard;
    if (!dashboardId && monitorId) {
      try {
        dashboard = await request('/status/api/dashboards', {method:'POST', body:JSON.stringify({monitor_id:monitorId})});
      } catch (error) {
        // A bookmark for a deleted monitor should still open the shared panel.
        if (error.status !== 404) throw error;
        dashboard = await request('/status/api/dashboards/default');
      }
    } else dashboard = await request(`/status/api/dashboards/${requestedDashboard}`);
    if (expectedRevision !== revision || busy) return;
    const selected = dashboard.monitors.some((item) => item.id === monitorId) ? monitorId : dashboard.monitors[0]?.id || null;
    dashboardId = dashboard.id;
    monitors = dashboard.monitors;
    schedulerOnline = dashboard.scheduler_online;
    monitorId = selected;
    if (model?.id !== selected) model = null;
    remember();
    networkError = '';
    detailError = '';
    ready = true;
    render();
    if (selected) {
      try {
        const fresh = await request(`/status/api/monitors/${selected}`);
        if (expectedRevision !== revision || busy) return;
        upsert(fresh);
      } catch (error) {
        if (expectedRevision !== revision || busy) return;
        model = null;
        detailError = `当前监测详情加载失败：${error.message}。可以切换其他监测或点击刷新重试。`;
      }
      render();
    }
  } catch (error) {
    if (expectedRevision !== revision) return;
    networkError = error.message;
    $('connection').textContent = '连接暂时中断';
    $('connection').className = 'connection error';
    notice(`${error.message}。服务器上已有的监测不会因为页面断线而停止。`);
  } finally {
    loading = false;
    $('refresh').disabled = busy;
    if (expectedRevision !== revision && !busy) refresh();
  }
}
function openSettings(mode = 'edit') {
  if (busy || !ready) return;
  const source = mode === 'new' ? null : model;
  editingId = mode === 'edit' ? source?.id || null : null;
  sourceId = mode === 'copy' ? source?.id || null : null;
  sourceUrl = source?.url || '';
  $('settings-title').textContent = editingId ? '编辑监测' : sourceId ? '复用连接 · 新增模型' : '新增监测';
  $('settings-intro').textContent = sourceId ? '沿用当前供应商的 URL 和 Key，填写另一个模型即可。保存后单独启停。' : '每个监测独立保存，关闭页面后服务器继续执行。';
  $('config-name').value = source ? ((source.name || '') + (sourceId && source.name ? ' · 副本' : '')).slice(0, 80) : '';
  $('config-url').value = source?.url || '';
  $('config-model').value = sourceId ? '' : source?.model || '';
  $('config-effort').value = source?.reasoning_effort || 'medium';
  $('config-interval').value = (source?.interval_seconds || 1800)/60;
  $('config-key').value = '';
  $('config-published').checked = mode === 'edit' && Boolean(source?.published);
  $('config-public-name').value = mode === 'edit' ? source?.public_name || '' : '';
  $('config-public-order').value = mode === 'edit' ? source?.public_order || 0 : 0;
  updateKeyHint();
  $('settings-error').hidden = true;
  $('settings-dialog').showModal();
}
function updateKeyHint() {
  const reuse = (editingId || sourceId) && normalizedUrl($('config-url').value) === sourceUrl;
  $('config-key').required = !reuse;
  $('config-key').placeholder = reuse ? '留空保留已保存的 Key' : '输入此接口的 Key';
  $('key-hint').textContent = reuse ? '由服务器保留，不回显' : (editingId || sourceId) ? '更换地址需填写新 Key' : '加密保存，不回显';
}
function lockSettings(locked) {
  for (const control of $('settings-form').querySelectorAll('input, select, button')) control.disabled = locked;
}
async function saveSettings(event) {
  event.preventDefault();
  if (busy || !ready) return;
  busy = true;
  const expectedRevision = ++revision;
  lockSettings(true);
  $('settings-error').hidden = true;
  const config = {name:$('config-name').value.trim(),url:$('config-url').value.trim(),model:$('config-model').value.trim(),reasoning_effort:$('config-effort').value,interval_seconds:Number($('config-interval').value)*60,published:$('config-published').checked,public_name:$('config-public-name').value.trim(),public_order:Number($('config-public-order').value)};
  if ($('config-key').value || (!editingId && !sourceId)) config.key = $('config-key').value;
  if (sourceId) config.source_monitor_id = sourceId;
  render();
  try {
    const saved = await request(editingId ? `/status/api/monitors/${editingId}` : `/status/api/dashboards/${dashboardId}/monitors`, {method:editingId ? 'PATCH' : 'POST',body:JSON.stringify(config)});
    if (expectedRevision !== revision) return;
    monitorId = saved.id;
    $('monitor-filter').value = '';
    upsert(saved);
    remember();
    $('config-key').value = '';
    networkError = '';
    $('settings-dialog').close();
  } catch (error) {
    if (expectedRevision !== revision) return;
    $('settings-error').textContent = error.message;
    $('settings-error').hidden = false;
  } finally { busy = false; lockSettings(false); render(); if (expectedRevision !== revision) refresh(); }
}
async function action(kind, id = monitorId) {
  const item = monitors.find((entry) => entry.id === id);
  if (busy || !ready || !item) return;
  busy = true;
  const expectedRevision = ++revision;
  render();
  try {
    const saved = kind === 'run' ? await request(`/status/api/monitors/${id}/run`, {method:'POST',body:'{}'}) : await request(`/status/api/monitors/${id}`, {method:'PATCH',body:JSON.stringify({enabled:!item.enabled})});
    if (expectedRevision !== revision) return;
    upsert(saved);
    networkError = '';
  } catch (error) { if (expectedRevision === revision) networkError = error.message; }
  finally { busy = false; render(); if (expectedRevision !== revision) refresh(); }
}
function openDelete(id) {
  const item = monitors.find((entry) => entry.id === id);
  if (busy || !ready || !item) return;
  deletingId = id;
  $('delete-name').textContent = title(item);
  $('delete-active-note').hidden = !pending(item);
  $('delete-error').hidden = true;
  $('delete-dialog').showModal();
}
async function deleteMonitor(event) {
  event.preventDefault();
  if (busy || !ready || !deletingId) return;
  const id = deletingId;
  busy = true;
  const expectedRevision = ++revision;
  for (const control of $('delete-form').querySelectorAll('button')) control.disabled = true;
  $('delete-error').hidden = true;
  render();
  try {
    try { await request(`/status/api/monitors/${id}`, {method:'DELETE'}); }
    catch (error) { if (error.status !== 404) throw error; }
    if (expectedRevision !== revision) return;
    const index = monitors.findIndex((item) => item.id === id);
    monitors = monitors.filter((item) => item.id !== id);
    if (monitorId === id) {
      monitorId = monitors[Math.max(0, Math.min(index, monitors.length - 1))]?.id || null;
      model = null;
      $('monitor-filter').value = '';
      $('history-dialog').close();
    }
    networkError = '';
    detailError = '';
    remember();
    $('delete-dialog').close();
  } catch (error) {
    if (expectedRevision !== revision) return;
    $('delete-error').textContent = error.message;
    $('delete-error').hidden = false;
  } finally {
    busy = false;
    for (const control of $('delete-form').querySelectorAll('button')) control.disabled = false;
    render();
    refresh();
  }
}
function select(id) {
  if (id === monitorId || busy || !ready) return;
  revision++;
  monitorId = id;
  model = null;
  detailError = '';
  $('history-dialog').close();
  remember();
  render();
  refresh();
}
function detail(id) {
  const item = model?.history.find((row) => row.id === id);
  if (!item) return;
  $('history-detail').innerHTML = `<div class="detail-summary"><span class="badge ${badge(item.status)}">${labels[item.status]}</span><p>${escape(item.result?.prediction_name || '未获得有效归因')} · ${pct(item.result?.probability)}</p><p class="detail-note">${escape(date(item.finished_at))} · 请求 ${escape(item.requested_model)} · 思考强度 ${escape(item.reasoning_effort)}<br>有效样本 ${item.progress.valid}/3 · ${escape(reasons[item.reason] || item.reason || '')}<br>任务 ID：${escape(item.id)}</p>${item.result?.sample_warning ? `<p class="notice">${escape(item.result.sample_warning)}</p>` : ''}</div><div id="detail-candidates"></div>${item.errors.length ? `<div class="detail-errors">${item.errors.map((error) => `第 ${error.attempt} 次：${escape(error.message)}`).join('<br>')}</div>` : ''}<p class="detail-note">指纹库版本：${escape(item.bank_sha256 || '—')}</p>`;
  renderCandidates(item.result, $('detail-candidates'));
  $('history-dialog').showModal();
}
$('settings-open').addEventListener('click', () => openSettings('edit'));
$('monitor-add').addEventListener('click', () => openSettings('new'));
$('monitor-copy').addEventListener('click', () => openSettings('copy'));
$('config-url').addEventListener('input', updateKeyHint);
$('monitor-filter').addEventListener('input', renderList);
$('monitor-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  if (button.dataset.action === 'select') select(button.dataset.id);
  else if (button.dataset.action === 'delete') openDelete(button.dataset.id);
  else action(button.dataset.action, button.dataset.id);
});
$('settings-form').addEventListener('submit', saveSettings);
$('delete-form').addEventListener('submit', deleteMonitor);
$('toggle').addEventListener('click', () => action('toggle'));
$('run').addEventListener('click', () => action('run'));
$('refresh').addEventListener('click', refresh);
$('history').addEventListener('click', (event) => { const row = event.target.closest('[data-id]'); if (row) detail(row.dataset.id); });
$('history').addEventListener('keydown', (event) => { if (['Enter',' '].includes(event.key) && event.target.dataset.id) { event.preventDefault(); detail(event.target.dataset.id); } });
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $(button.dataset.close).close());
$('settings-dialog').addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
$('settings-dialog').addEventListener('close', () => { $('config-key').value = ''; });
$('delete-dialog').addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
$('delete-dialog').addEventListener('close', () => { deletingId = null; });
function initialize() {
  revision++;
  ready = false;
  monitors = [];
  model = null;
  schedulerOnline = false;
  networkError = '';
  detailError = '';
  $('settings-dialog').close();
  $('history-dialog').close();
  $('delete-dialog').close();
  const fragment = new URLSearchParams(location.hash.slice(1));
  dashboardId = fragment.get('dashboard');
  monitorId = fragment.get('monitor');
  if (!validId(dashboardId)) dashboardId = null;
  if (!validId(monitorId)) monitorId = null;
  sharedDefault = !dashboardId;
  render();
  refresh();
}
window.addEventListener('hashchange', initialize);
initialize();
setInterval(refresh, 3000);
setInterval(countdown, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
