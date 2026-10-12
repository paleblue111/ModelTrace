let sessionPromise = null;
async function session() {
  if (!sessionPromise) sessionPromise = (async () => {
    const response = await fetch('/admin/api/session', {signal:AbortSignal.timeout(15000)});
    if (response.status === 401) { location.replace('/admin/login'); throw new Error('管理会话已过期，请重新登录'); }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '无法连接管理后台');
    return data;
  })().catch(error => { sessionPromise = null; throw error; });
  return sessionPromise;
}
export async function adminFetch(url, options = {}) {
  const data = await session();
  const response = await fetch(url, {...options, headers:{'Content-Type':'application/json', 'X-CSRF-Token':data.csrf, ...options.headers}});
  if (response.status === 401) { sessionPromise = null; location.replace('/admin/login'); }
  return response;
}
export async function adminRequest(url, options = {}) {
  const response = await adminFetch(url, {...options, signal:AbortSignal.timeout(15000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
  return data;
}
