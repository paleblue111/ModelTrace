const $ = (id) => document.getElementById(id);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'排队中',running:'检测中',completed:'完整结果',partial:'样本不足',failed:'检测失败',cancelled:'已取消',interrupted:'执行中断'};
const reasons = {target_reached:'已收集三份有效回答',attempts_exhausted:'已达到本轮尝试次数上限',upstream_error:'上游请求失败',task_timeout:'达到任务总时限',queue_timeout:'排队超时',user_cancelled:'用户取消',worker_lost:'执行进程中断',worker_shutdown:'执行进程关闭',worker_error:'执行异常'};
const pct = (value) => typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '—';
const duration = (value) => typeof value !== 'number' ? '—' : value < 60 ? `${Math.round(value)} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
const date = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const badge = (status) => ['completed'].includes(status) ? 'success' : ['partial','interrupted'].includes(status) ? 'warning' : status === 'failed' ? 'danger' : 'neutral';
let monitorId = new URLSearchParams(location.hash.slice(1)).get('monitor');
try { monitorId ||= localStorage.getItem('modeltrace-monitor'); } catch { /* storage is optional */ }
if (monitorId && !/^[0-9a-f]{32}$/.test(monitorId)) monitorId = null;
let model = null;
let busy = false;
let loading = false;
let networkError = '';

async function request(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {...options, headers:{'Content-Type':'application/json', ...options.headers}, signal:controller.signal});
    let data;
    try { data = await response.json(); } catch { throw new Error('服务器未返回有效数据，请检查服务是否启动'); }
    if (!response.ok) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('连接超时，请刷新确认服务器状态；不要重复提交配置');
    throw error;
  } finally { clearTimeout(timeout); }
}
function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}
function remember(id) {
  monitorId = id;
  try { localStorage.setItem('modeltrace-monitor', id); } catch { /* ID is also in the address */ }
  // The capability ID stays in the fragment, which is not sent as a Referer.
  history.replaceState(null, '', `${location.pathname}#monitor=${id}`);
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
  if (!model) return;
  const active = model.active_task;
  const latest = model.history[0];
  const result = latest?.result;
  const pending = Boolean(model.active_task_id || model.run_requested);
  $('connection').textContent = networkError ? '连接暂时中断' : model.scheduler_online ? '调度服务在线' : '调度服务离线';
  $('connection').className = `connection ${!networkError && model.scheduler_online ? 'online' : 'error'}`;
  $('monitor-badge').textContent = model.enabled ? '持续监测' : '已暂停';
  $('monitor-badge').className = `badge ${model.enabled ? 'success' : 'neutral'}`;
  $('monitor-title').textContent = pending ? '正在收集模型指纹' : model.enabled ? '监测已开启' : '随时开始下一次检测';
  $('monitor-detail').textContent = pending ? '每轮独立采样，结果会在完成后自动更新。' : model.enabled ? '后台按计划执行，你可以放心关闭这个页面。' : '配置已保存在服务器，可立即检测或开启定时监测。';
  $('target-model').textContent = model.model;
  $('target-effort').textContent = model.reasoning_effort;
  $('toggle').textContent = model.enabled ? '暂停定时监测' : '开启定时监测';
  $('toggle').disabled = busy;
  $('run').disabled = busy || pending;
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
  if (model.history.length) {
    $('history').innerHTML = model.history.map((item) => `<tr data-id="${escape(item.id)}" tabindex="0" role="button" aria-label="查看 ${escape(date(item.finished_at))} 的检测详情"><td>${escape(date(item.finished_at))}</td><td>${escape(item.requested_model)}</td><td>${escape(item.result?.prediction_name || '—')}</td><td>${pct(item.result?.probability)}</td><td>${item.progress.valid} / 3</td><td><span class="badge ${badge(item.status)}">${labels[item.status] || '未知'}</span></td><td>${duration(item.elapsed_seconds)}</td></tr>`).join('');
  }
  const warnings = [networkError, model.last_error];
  if (!model.scheduler_online) warnings.push('调度服务暂未在线，检查 Docker 中的 scheduler 服务。页面不会替代服务器发起定时检测。');
  notice(warnings.filter(Boolean).join('；'));
  countdown();
}
async function refresh() {
  if (!monitorId || loading || busy) return;
  loading = true;
  try {
    const fresh = await request(`/status/api/monitors/${monitorId}`);
    if (busy || (model && fresh.updated_at < model.updated_at)) return;
    model = fresh;
    networkError = '';
    render();
  } catch (error) {
    networkError = error.message;
    $('connection').textContent = '连接暂时中断';
    $('connection').className = 'connection error';
    notice(`${error.message}。服务器上已有的监测不会因为页面断线而停止。`);
  } finally { loading = false; }
}
function openSettings() {
  $('config-url').value = model?.url || '';
  $('config-model').value = model?.model || '';
  $('config-effort').value = model?.reasoning_effort || 'medium';
  $('config-interval').value = (model?.interval_seconds || 1800)/60;
  $('config-key').value = '';
  $('config-key').required = !model;
  $('config-key').placeholder = model ? '留空保留已保存的 Key' : '输入上游服务的 Key';
  $('key-hint').textContent = model ? '已加密保存 · 留空不修改' : '加密保存，不回显';
  $('settings-error').hidden = true;
  $('settings-dialog').showModal();
}
async function saveSettings(event) {
  event.preventDefault();
  if (busy) return;
  busy = true;
  $('save').disabled = true;
  $('settings-error').hidden = true;
  const config = {url:$('config-url').value.trim(),model:$('config-model').value.trim(),reasoning_effort:$('config-effort').value,interval_seconds:Number($('config-interval').value)*60};
  if ($('config-key').value || !model) config.key = $('config-key').value;
  try {
    const saved = await request(model ? `/status/api/monitors/${monitorId}` : '/status/api/monitors', {method:model ? 'PATCH' : 'POST',body:JSON.stringify(config)});
    model = saved;
    remember(saved.id);
    $('config-key').value = '';
    networkError = '';
    $('settings-dialog').close();
  } catch (error) {
    $('settings-error').textContent = error.message;
    $('settings-error').hidden = false;
  } finally { busy = false; $('save').disabled = false; render(); }
}
async function action(kind) {
  if (busy || !model) return;
  busy = true;
  render();
  try {
    model = kind === 'run' ? await request(`/status/api/monitors/${monitorId}/run`, {method:'POST',body:'{}'}) : await request(`/status/api/monitors/${monitorId}`, {method:'PATCH',body:JSON.stringify({enabled:!model.enabled})});
    networkError = '';
  } catch (error) { networkError = error.message; }
  finally { busy = false; render(); }
}
function detail(id) {
  const item = model?.history.find((row) => row.id === id);
  if (!item) return;
  $('history-detail').innerHTML = `<div class="detail-summary"><span class="badge ${badge(item.status)}">${labels[item.status]}</span><p>${escape(item.result?.prediction_name || '未获得有效归因')} · ${pct(item.result?.probability)}</p><p class="detail-note">${escape(date(item.finished_at))} · 请求 ${escape(item.requested_model)} · 思考强度 ${escape(item.reasoning_effort)}<br>有效样本 ${item.progress.valid}/3 · ${escape(reasons[item.reason] || item.reason || '')}<br>任务 ID：${escape(item.id)}</p>${item.result?.sample_warning ? `<p class="notice">${escape(item.result.sample_warning)}</p>` : ''}</div><div id="detail-candidates"></div>${item.errors.length ? `<div class="detail-errors">${item.errors.map((error) => `第 ${error.attempt} 次：${escape(error.message)}`).join('<br>')}</div>` : ''}<p class="detail-note">指纹库版本：${escape(item.bank_sha256 || '—')}</p>`;
  renderCandidates(item.result, $('detail-candidates'));
  $('history-dialog').showModal();
}
$('settings-open').addEventListener('click', openSettings);
$('settings-form').addEventListener('submit', saveSettings);
$('toggle').addEventListener('click', () => action('toggle'));
$('run').addEventListener('click', () => action('run'));
$('refresh').addEventListener('click', refresh);
$('history').addEventListener('click', (event) => { const row = event.target.closest('[data-id]'); if (row) detail(row.dataset.id); });
$('history').addEventListener('keydown', (event) => { if (['Enter',' '].includes(event.key) && event.target.dataset.id) { event.preventDefault(); detail(event.target.dataset.id); } });
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $(button.dataset.close).close());
$('settings-dialog').addEventListener('close', () => { $('config-key').value = ''; });
if (monitorId) { remember(monitorId); refresh(); }
setInterval(refresh, 3000);
setInterval(countdown, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
