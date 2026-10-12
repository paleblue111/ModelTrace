const $ = (id) => document.getElementById(id);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'排队中',running:'检测中',completed:'完整结果',partial:'样本不足',failed:'检测失败',cancelled:'已取消',interrupted:'执行中断'};
const terminal = new Set(['completed','partial','failed','cancelled','interrupted']);
const pct = value => typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '—';
const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const statusBadge = status => `<span class="badge ${status === 'completed' ? 'success' : status === 'partial' ? 'warning' : status === 'failed' ? 'danger' : 'neutral'}">${escape(labels[status] || '等待检测')}</span>`;
let monitors = [], selected = null, monitor = null, records = [], page = 1, pages = 1, loading = false, submitting = false, publicEnabled = false, dialogId = null, revision = 0;
async function request(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {...options, headers:{'Content-Type':'application/json', ...options.headers}, signal:controller.signal});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || '请求失败，请稍后重试'); error.status = response.status; throw error; }
    return data;
  } catch (error) { if (error.name === 'AbortError') throw new Error('连接超时，请刷新查看记录，避免重复提交。'); throw error; }
  finally { clearTimeout(timer); }
}
function resultMarkup(item) {
  const result = item.result;
  const elapsed = item.elapsed_seconds;
  const duration = elapsed < 60 ? `${Math.round(elapsed)} 秒` : `${Math.floor(elapsed / 60)} 分 ${Math.round(elapsed % 60)} 秒`;
  return `<div class="public-result">${statusBadge(item.status)}<h3>${escape(result?.prediction_name || (terminal.has(item.status) ? '本轮未获得归因结果' : '正在收集模型指纹'))}</h3><p class="public-result-note">${escape(result?.sample_warning || item.note || '完成检测后，归因结果会自动更新。')}</p><div class="result-metrics"><div><span>库内归因概率</span><strong>${pct(result?.probability)}</strong></div><div><span>有效样本</span><strong>${item.progress.valid}<small> / 3</small></strong></div><div><span>检测耗时</span><strong>${escape(duration)}</strong></div></div>${result?.results?.length ? `<div class="public-candidates">${result.results.map((candidate,index) => `<div class="candidate"><span class="rank">${String(index+1).padStart(2,'0')}</span><div class="candidate-main"><div class="candidate-name">${escape(candidate.display_name)}</div><div class="bar"><i style="width:${Math.max(0,Math.min(100,Number(candidate.probability)*100))}%"></i></div></div><span class="candidate-score">${pct(candidate.probability)}</span></div>`).join('')}</div>` : ''}</div>`;
}
function renderMonitors() {
  const focusId = document.activeElement?.closest('[data-monitor]')?.dataset.monitor;
  $('featured-count').textContent = monitors.length;
  $('monitor-list').innerHTML = monitors.length ? monitors.map(item => `<article class="monitor-tile ${item.id === selected ? 'selected' : ''}"><button type="button" class="monitor-select" data-monitor="${escape(item.id)}" aria-pressed="${item.id === selected}"><span class="tile-heading"><strong>${escape(item.name)}</strong><span class="badge ${item.enabled ? 'success' : 'neutral'}">${item.enabled ? '持续监测' : '已暂停'}</span></span><span class="tile-model">${escape(item.model)} · ${escape(item.reasoning_effort)}</span><span class="tile-result">${item.latest ? `<b>${escape(item.latest.result?.prediction_name || labels[item.latest.status])}</b> · ${pct(item.latest.result?.probability)}<br>${escape(date(item.latest.finished_at))} · 样本 ${item.latest.progress.valid}/3` : '等待第一轮检测'}</span></button></article>`).join('') : '<div class="empty"><strong>暂无公开监测</strong><p>管理员精选的监测将在这里显示。</p></div>';
  if (focusId) $('monitor-list').querySelector(`[data-monitor="${focusId}"]`)?.focus({preventScroll:true});
}
function renderMonitor() {
  if (!monitor) {
    $('featured-detail').innerHTML = `<div class="empty"><strong>${monitors.length ? '正在加载精选监测' : '暂无公开监测'}</strong><p>${monitors.length ? '请稍候，结果正在更新。' : '管理员公开监测后，最新结果将在这里显示。'}</p></div>`;
    return;
  }
  const focused = document.activeElement?.dataset.historyIndex;
  $('featured-detail').innerHTML = `<div class="featured-header"><div><h3>${escape(monitor.name)}</h3><p>${escape(monitor.model)} · ${escape(monitor.reasoning_effort)} · 每 ${monitor.interval_seconds/60} 分钟</p></div><span class="badge ${monitor.enabled ? 'success' : 'neutral'}">${monitor.enabled ? '持续监测' : '已暂停'}</span></div>${monitor.latest ? resultMarkup(monitor.latest) : '<div class="empty"><strong>等待第一轮检测</strong><p>结果完成后会自动更新。</p></div>'}${monitor.history.length ? `<div class="table-wrap featured-history"><table><thead><tr><th>完成时间</th><th>指纹归因</th><th>概率</th><th>样本</th><th>状态</th></tr></thead><tbody>${monitor.history.map((item,index) => `<tr data-history-index="${index}" tabindex="0" role="button" aria-label="查看 ${escape(date(item.finished_at))} 的检测详情"><td>${escape(date(item.finished_at))}</td><td>${escape(item.result?.prediction_name || '—')}</td><td>${pct(item.result?.probability)}</td><td>${item.progress.valid}/3</td><td>${statusBadge(item.status)}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
  if (focused !== undefined) $('featured-detail').querySelector(`[data-history-index="${focused}"]`)?.focus({preventScroll:true});
}
function renderFeed(total) {
  const focused = document.activeElement?.closest('[data-public-id]')?.dataset.publicId;
  $('public-total').textContent = total;
  $('public-page').textContent = `第 ${page} / ${pages} 页`;
  $('public-prev').disabled = page <= 1;
  $('public-next').disabled = page >= pages;
  $('public-history').innerHTML = records.length ? records.map(item => `<tr data-public-id="${escape(item.id)}" tabindex="0" role="button" aria-label="查看 ${escape(item.model)} 的检测详情"><td>${escape(date(item.created_at))}</td><td>${escape(item.model)}</td><td>${escape(item.result?.prediction_name || '—')}</td><td>${pct(item.result?.probability)}</td><td>${item.progress.valid}/3</td><td>${statusBadge(item.status)}</td><td>${escape(date(item.finished_at))}</td></tr>`).join('') : '<tr><td colspan="7"><div class="empty history-empty"><strong>还没有公开检测记录</strong><p>提交第一轮检测，结果将自动更新。</p></div></td></tr>';
  if (focused) $('public-history').querySelector(`[data-public-id="${focused}"]`)?.focus({preventScroll:true});
}
async function refresh() {
  if (loading) return;
  loading = true;
  $('public-refresh').disabled = true;
  const expected = revision;
  const errors = [];
  try {
    const [home, feed] = await Promise.allSettled([request('/public/api/home'), request(`/public/api/detections?page=${page}`)]);
    if (expected !== revision) return;
    if (home.status === 'fulfilled') {
      const data = home.value;
      $('home-title').textContent = data.settings.title;
      $('home-description').textContent = data.settings.description;
      $('home-announcement').textContent = data.settings.announcement;
      $('home-announcement').hidden = !data.settings.announcement;
      publicEnabled = data.settings.public_enabled;
      $('public-test-fields').disabled = submitting || !data.settings.public_enabled;
      if (!data.settings.public_enabled) { $('test-error').textContent = '管理员暂未开放单次检测，已有结果仍可查看。'; $('test-error').hidden = false; }
      else if ($('test-error').textContent.startsWith('管理员暂未开放')) $('test-error').hidden = true;
      monitors = data.monitors;
      selected = monitors.some(item => item.id === selected) ? selected : monitors[0]?.id || null;
      if (monitor?.id !== selected) monitor = null;
      renderMonitors(); renderMonitor();
      if (selected) {
        try { const fresh = await request(`/public/api/monitors/${selected}`); if (expected !== revision) return; monitor = fresh; renderMonitor(); }
        catch (error) { if (expected !== revision) return; monitor = null; renderMonitor(); errors.push(error.message); }
      }
    } else errors.push(home.reason.message);
    if (feed.status === 'fulfilled') {
      records = feed.value.items; page = feed.value.page; pages = feed.value.pages;
      renderFeed(feed.value.total);
    } else errors.push(feed.reason.message);
    if (dialogId && $('public-detail-dialog').open) {
      const requestedDialog = dialogId;
      try { const item = await request(`/public/api/detections/${requestedDialog}`); if (expected === revision && dialogId === requestedDialog && $('public-detail-dialog').open) $('public-detail-content').innerHTML = `<p class="detail-note">请求 ${escape(item.model)} · ${escape(item.reasoning_effort)} · ${escape(date(item.created_at))}</p>${resultMarkup(item)}`; }
      catch (error) { if (expected === revision && dialogId === requestedDialog && $('public-detail-dialog').open) $('public-detail-content').textContent = error.message; }
    }
    if (expected === revision) { $('public-notice').textContent = errors.join('；'); $('public-notice').hidden = !errors.length; }
  } finally { loading = false; $('public-refresh').disabled = false; if (expected !== revision) refresh(); }
}
async function openDetection(id) {
  dialogId = id;
  $('public-detail-content').textContent = '正在加载检测详情…';
  if (!$('public-detail-dialog').open) $('public-detail-dialog').showModal();
  try { const item = await request(`/public/api/detections/${id}`); if (dialogId === id && $('public-detail-dialog').open) $('public-detail-content').innerHTML = `<p class="detail-note">请求 ${escape(item.model)} · ${escape(item.reasoning_effort)} · ${escape(date(item.created_at))}</p>${resultMarkup(item)}`; }
  catch (error) { if (dialogId === id) $('public-detail-content').textContent = error.message; }
}
$('public-test-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting || !publicEnabled) return;
  submitting = true;
  $('public-test-fields').disabled = true;
  $('test-error').hidden = true;
  $('test-success').hidden = true;
  try {
    const saved = await request('/public/api/detections', {method:'POST', body:JSON.stringify({url:$('test-url').value.trim(),key:$('test-key').value,model:$('test-model').value.trim(),reasoning_effort:$('test-effort').value})});
    page = 1; revision++;
    $('test-success').textContent = '检测已提交，进度与结果会在公共列表自动更新。';
    $('test-success').hidden = false;
    await openDetection(saved.id);
  } catch (error) { $('test-error').textContent = error.message; $('test-error').hidden = false; }
  finally { $('test-key').value = ''; submitting = false; $('public-test-fields').disabled = !publicEnabled; refresh(); }
});
$('monitor-list').addEventListener('click', event => { const item = event.target.closest('[data-monitor]'); if (item && item.dataset.monitor !== selected) { selected = item.dataset.monitor; monitor = null; revision++; renderMonitors(); renderMonitor(); history.replaceState(null,'', '#monitor=' + selected); refresh(); } });
function selectHistory(event) { const row = event.target.closest('[data-history-index]'); const item = row && monitor?.history[Number(row.dataset.historyIndex)]; if (item) { dialogId = null; $('public-detail-content').innerHTML = `<p class="detail-note">请求 ${escape(item.model)} · ${escape(date(item.finished_at))}</p>${resultMarkup(item)}`; $('public-detail-dialog').showModal(); } }
$('featured-detail').addEventListener('click', selectHistory);
$('featured-detail').addEventListener('keydown', event => { if (['Enter',' '].includes(event.key) && event.target.hasAttribute('data-history-index')) { event.preventDefault(); selectHistory(event); } });
$('public-history').addEventListener('click', event => { const row = event.target.closest('[data-public-id]'); if (row) openDetection(row.dataset.publicId); });
$('public-history').addEventListener('keydown', event => { if (['Enter',' '].includes(event.key) && event.target.dataset.publicId) { event.preventDefault(); openDetection(event.target.dataset.publicId); } });
$('public-detail-close').addEventListener('click', () => $('public-detail-dialog').close());
$('public-detail-dialog').addEventListener('close', () => { dialogId = null; });
$('public-refresh').addEventListener('click', refresh);
$('public-prev').addEventListener('click', () => { if (page > 1) { page--; revision++; refresh(); } });
$('public-next').addEventListener('click', () => { if (page < pages) { page++; revision++; refresh(); } });
selected = new URLSearchParams(location.hash.slice(1)).get('monitor');
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
