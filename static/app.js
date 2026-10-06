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
const learningForm = document.querySelector('[data-learning-form]');
document.querySelectorAll('[data-topic]').forEach(button => button.addEventListener('click', () => {
  const input = document.querySelector('#learning-topic');
  input.value = button.dataset.topic;
  input.focus();
}));
const learningWait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function pollLearning(url, target) {
  target.textContent = '正在检索与整理，通常需要几分钟。可以关闭网页，稍后在最近任务中查看。';
  for (let attempt=0; attempt<190; attempt++) {
    const response = await fetch(url, {headers:{Accept:'application/json'}});
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('无法查看任务，请刷新页面或重新登录。');
    const job = await response.json();
    if (job.status==='error') throw new Error(job.error || '整理未完成，请重试。');
    if (job.status==='ready') {
      target.textContent = '草稿已生成，尚未保存。';
      const link = document.createElement('a');
      link.className = 'button secondary'; link.href = job.review_url; link.textContent = '审核并保存知识卡';
      target.append(' ', link);
      return;
    }
    await learningWait(3000);
  }
  throw new Error('页面等待已结束。请刷新查看最近任务，未自动重复调用助手。');
}
learningForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = learningForm.querySelector('[data-learning-submit]');
  const target = learningForm.querySelector('[data-learning-status]');
  let jobStatus;
  button.disabled=true; target.textContent='正在提交学习主题…';
  try {
    const response = await fetch(learningForm.action, {method:'POST', body:new FormData(learningForm), headers:{Accept:'application/json'}});
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('页面已过期，请刷新或重新登录。');
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '提交失败，请重试。');
    const row = document.createElement('div'); row.className='learning-job';
    const title = document.createElement('strong'); title.textContent=learningForm.elements.topic.value;
    jobStatus = document.createElement('span'); jobStatus.className='small muted'; jobStatus.textContent='检索与整理中…';
    row.append(title, jobStatus);
    document.querySelector('[data-learning-empty]')?.remove();
    document.querySelector('[data-learning-jobs]')?.prepend(row);
    await pollLearning(result.job_url, target);
  } catch(error) { target.textContent=error.message; }
  finally {
    button.disabled=false;
    if (jobStatus) jobStatus.replaceChildren(...Array.from(target.childNodes,node=>node.cloneNode(true)));
  }
});
document.querySelectorAll('[data-learning-job]').forEach(row => {
  pollLearning(row.dataset.learningJob, row.querySelector('[data-job-status]')).catch(error => {
    row.querySelector('[data-job-status]').textContent=error.message;
  });
});
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
