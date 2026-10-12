const {adminRequest} = await import(document.body.dataset.adminClient);
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fields = ['title','description','announcement','public_enabled','public_hourly_limit','public_pending_limit','public_queue_limit','public_concurrency'];
const labels = {queued:'排队中',running:'检测中',completed:'完整结果',partial:'样本不足',failed:'检测失败',cancelled:'已取消',interrupted:'执行中断'};
const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
let page = 1, pages = 1, loading = false, busy = false, loaded = false;
function error(message) { $('site-error').textContent = message; $('site-error').hidden = !message; }
async function loadSettings() {
  try {
    const saved = await adminRequest('/admin/api/site');
    for (const field of fields) { const input = $('site-' + field); if (field === 'public_enabled') input.checked = saved[field]; else input.value = saved[field]; }
    loaded = true;
    $('site-save').disabled = false;
    error('');
  } catch (failure) { error(failure.message); }
}
async function refreshFeed() {
  if (loading || busy) return;
  loading = true;
  $('moderation-refresh').disabled = true;
  try {
    const feed = await adminRequest(`/admin/api/detections?page=${page}`);
    page = feed.page; pages = feed.pages;
    const focused = document.activeElement?.dataset.moderate;
    $('moderation-rows').innerHTML = feed.items.length ? feed.items.map(item => `<tr><td>${escape(date(item.created_at))}</td><td>${escape(item.model)}</td><td>${escape(item.result?.prediction_name || '—')}</td><td>${escape(labels[item.status] || '未知')}</td><td><span class="badge ${item.hidden ? 'neutral' : 'success'}">${item.hidden ? '已隐藏' : '公开'}</span></td><td><button class="button secondary" type="button" data-moderate="${escape(item.id)}" data-hidden="${item.hidden}">${item.hidden ? '恢复公开' : '隐藏记录'}</button></td></tr>`).join('') : '<tr><td colspan="6"><div class="empty history-empty"><strong>还没有网站公共检测记录</strong></div></td></tr>';
    if (focused) $('moderation-rows').querySelector(`[data-moderate="${focused}"]`)?.focus({preventScroll:true});
    $('moderation-count').textContent = feed.total;
    $('moderation-page').textContent = `第 ${page} / ${pages} 页`;
    $('moderation-prev').disabled = page <= 1;
    $('moderation-next').disabled = page >= pages;
    $('moderation-error').hidden = true;
  } catch (failure) { $('moderation-error').textContent = failure.message; $('moderation-error').hidden = false; }
  finally { loading = false; $('moderation-refresh').disabled = false; }
}
$('site-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !loaded) return;
  busy = true;
  const config = {};
  for (const field of fields) config[field] = field === 'public_enabled' ? $('site-' + field).checked : field.startsWith('public_') ? Number($('site-' + field).value) : $('site-' + field).value.trim();
  for (const input of $('site-form').querySelectorAll('input,textarea,button')) input.disabled = true;
  error(''); $('site-saved').hidden = true;
  try { await adminRequest('/admin/api/site', {method:'PATCH',body:JSON.stringify(config)}); $('site-saved').hidden = false; }
  catch (failure) { error(failure.message); }
  finally { busy = false; for (const input of $('site-form').querySelectorAll('input,textarea,button')) input.disabled = false; }
});
$('moderation-rows').addEventListener('click', async event => {
  const button = event.target.closest('[data-moderate]');
  if (!button || busy) return;
  busy = true; button.disabled = true;
  try { await adminRequest(`/admin/api/detections/${button.dataset.moderate}`, {method:'PATCH',body:JSON.stringify({hidden:button.dataset.hidden !== 'true'})}); }
  catch (failure) { $('moderation-error').textContent = failure.message; $('moderation-error').hidden = false; }
  finally { busy = false; button.disabled = false; refreshFeed(); }
});
$('admin-logout').addEventListener('click', async () => {
  $('admin-logout').disabled = true;
  try { await adminRequest('/admin/api/logout', {method:'POST',body:'{}'}); location.replace('/admin/login'); }
  catch (failure) { error(failure.message); $('admin-logout').disabled = false; }
});
$('site-reload').addEventListener('click', () => { if (!busy) loadSettings(); });
for (const [link, target] of [['site-jump','site-settings'], ['moderation-jump','public-management']]) {
  $(link).addEventListener('click', event => { event.preventDefault(); $(target).scrollIntoView({block:'start'}); });
}
$('moderation-refresh').addEventListener('click', refreshFeed);
$('moderation-prev').addEventListener('click', () => { if (page > 1 && !loading) { page--; refreshFeed(); } });
$('moderation-next').addEventListener('click', () => { if (page < pages && !loading) { page++; refreshFeed(); } });
loadSettings(); refreshFeed();
setInterval(() => { if (!document.hidden) refreshFeed(); }, 5000);
