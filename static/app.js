const toggle = document.querySelector('.menu-toggle');
toggle?.addEventListener('click', () => {
  const expanded = document.body.classList.toggle('nav-open');
  toggle.setAttribute('aria-expanded', String(expanded));
});
document.addEventListener('keydown', event => {
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
    const search = document.querySelector('.global-search input');
    if (search) { event.preventDefault(); search.focus(); }
  }
  if (event.key === 'Escape') { document.body.classList.remove('nav-open'); toggle?.setAttribute('aria-expanded','false'); }
});
const editor = document.querySelector('[data-editor]');
const githubAuth = document.querySelector('[data-github-auth]');
if (githubAuth) {
  const form = githubAuth.querySelector('[data-github-login-form]');
  const button = githubAuth.querySelector('[data-github-login-button]');
  const message = githubAuth.querySelector('[data-github-message]');
  const device = githubAuth.querySelector('[data-github-device]');
  const code = githubAuth.querySelector('#github-device-code');
  const copy = githubAuth.querySelector('[data-github-copy]');
  let timer;
  let failures = 0;
  const busy = state => ['checking', 'preparing', 'awaiting'].includes(state);
  const render = state => {
    clearTimeout(timer);
    githubAuth.dataset.state = state.status;
    message.textContent = state.message;
    button.disabled = busy(state.status);
    button.textContent = state.status === 'signed_in' ? '检查登录状态' : busy(state.status) ? '正在登录…' : state.status === 'error' ? '重新登录 GitHub' : '登录 GitHub';
    code.value = state.code || '';
    device.hidden = !state.code;
    copy.textContent = '复制授权码';
    if (busy(state.status)) timer = setTimeout(poll, 2000);
  };
  const read = async response => {
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('session');
    return response.json();
  };
  const poll = async () => {
    try {
      render(await read(await fetch(githubAuth.dataset.statusUrl, {cache:'no-store'})));
      failures = 0;
    } catch (error) {
      failures += 1;
      if (failures < 3) timer = setTimeout(poll, 3000);
      else render({status:'error', message:'无法获取登录状态。请刷新页面确认本机访问登录及网络后重试。'});
    }
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    render({status:'preparing', message:'正在准备 GitHub 登录…'});
    try {
      failures = 0;
      render(await read(await fetch(form.action, {method:'POST', body:new FormData(form)})));
    } catch (error) {
      render({status:'error', message:'启动登录失败，请刷新页面后重试。'});
    }
  });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.value);
      copy.textContent = '已复制';
    } catch (error) {
      code.focus(); code.select();
      copy.textContent = '请手动复制选中的码';
    }
  });
  poll();
}
document.querySelector('[data-sync-form]')?.addEventListener('submit', () => {
  const button = document.querySelector('[data-sync-button]');
  button.disabled = true;
  button.textContent = '正在同步，请稍候…';
});
if (editor) {
  const content = document.querySelector('#content');
  let dirty = false;
  const count = () => { document.querySelector('#word-count').textContent = `${content.value.length.toLocaleString()} 字符`; };
  count();
  editor.addEventListener('input', () => { dirty = true; count(); });
  editor.addEventListener('change', () => { dirty = true; });
  editor.addEventListener('submit', () => { dirty = false; });
  window.addEventListener('beforeunload', event => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  const tools = { heading: ['## ', ''], bold: ['**','**'], list: ['- ', ''], code: ['\n```\n','\n```\n'], link: ['[','](https://)'] };
  document.querySelectorAll('[data-insert]').forEach(button => button.addEventListener('click', () => {
    const [prefix,suffix] = tools[button.dataset.insert];
    const start = content.selectionStart, end = content.selectionEnd;
    const selection = content.value.slice(start,end);
    content.setRangeText(prefix + selection + suffix, start, end, 'end');
    content.focus();
    content.setSelectionRange(start + prefix.length, start + prefix.length + selection.length);
    content.dispatchEvent(new Event('input',{bubbles:true}));
  }));
}
