const form = document.getElementById('login-form');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('login-submit');
  const error = document.getElementById('login-error');
  if (button.disabled) return;
  button.disabled = true;
  error.hidden = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('/admin/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:document.getElementById('login-token').value}), signal:controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '登录失败，请稍后重试');
    document.getElementById('login-token').value = '';
    location.replace('/admin');
  } catch (failure) {
    error.textContent = failure.name === 'AbortError' ? '登录超时，请稍后重试' : failure.message;
    error.hidden = false;
  } finally { clearTimeout(timer); button.disabled = false; }
});
