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
const progressPanel = document.querySelector('[data-learning-progress]');
let learningPoll = 0, learningTimer;
function learningReviewLink(url) {
  const link = document.createElement('a');
  link.className='button secondary'; link.href=url; link.textContent='审核并保存知识卡';
  return link;
}
function displayLearning(job) {
  if (!progressPanel) return;
  const progress = job.progress || {};
  progressPanel.classList.toggle('is-running', job.status==='pending');
  progressPanel.classList.toggle('has-error', job.status==='error');
  progressPanel.querySelector('[data-learning-progress-topic]').textContent=job.topic;
  progressPanel.querySelector('[data-learning-current]').textContent = progress.detail || '等待任务进度…';
  progressPanel.setAttribute('aria-label', '整理过程：'+job.topic);
  const received = Date.now(), seconds = job.elapsed_seconds || 0;
  clearInterval(learningTimer);
  const clock = () => {
    const elapsed=seconds+(job.status==='pending'?Math.floor((Date.now()-received)/1000):0);
    progressPanel.querySelector('[data-learning-clock]').textContent = `${String(Math.floor(elapsed/60)).padStart(2,'0')}:${String(elapsed%60).padStart(2,'0')}`;
  };
  clock(); if (job.status==='pending') learningTimer=setInterval(clock,1000);
  const events=progress.events || [], visited=new Set(events.map(event=>event.stage));
  progressPanel.querySelectorAll('[data-stage]').forEach(step=>{
    step.classList.toggle('visited',visited.has(step.dataset.stage));
    step.classList.toggle('current',job.status==='pending' && step.dataset.stage===progress.stage);
  });
  const sources=progressPanel.querySelector('[data-learning-sources]');
  sources.replaceChildren();
  for (const source of progress.sources || []) {
    let url; try { url=new URL(source.url); } catch { continue; }
    if (url.protocol!=='https:' || url.username || url.password) continue;
    const card=document.createElement('div'); card.className='learning-source';
    const link=document.createElement('a'); link.href=url.href; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=source.title;
    const scope=document.createElement('p'); scope.className='small muted'; scope.textContent=source.scope;
    card.append(link,scope); sources.append(card);
  }
  const log=progressPanel.querySelector('[data-learning-events]');
  const fingerprint=JSON.stringify(events);
  if (log.dataset.events!==fingerprint) {
    const nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<35;
    log.replaceChildren(); log.dataset.events=fingerprint;
    for (const event of events) {
      const row=document.createElement('li'), stamp=document.createElement('time'), detail=document.createElement('span');
      const date=new Date(event.at); stamp.textContent=Number.isNaN(date.getTime())?'—':date.toLocaleTimeString('zh-CN',{hour12:false});
      detail.textContent=event.detail; row.append(stamp,detail); log.append(row);
    }
    if (nearBottom) log.scrollTop=log.scrollHeight;
  }
  const output=progressPanel.querySelector('[data-learning-output]');
  const html=progress.output_html || '';
  if (output.dataset.output!==html) {
    output.dataset.output=html;
    // This HTML is rendered and sanitized on the authenticated server, never raw CLI text.
    output.innerHTML=html || '<p class="muted">尚未收到公开正文。</p>';
    window.renderResearchMath?.(output);
  }
  const review=progressPanel.querySelector('[data-learning-review]'); review.replaceChildren();
  if (job.status==='ready' && job.review_url) review.append(learningReviewLink(job.review_url));
}
async function pollLearning(url, target) {
  const token=++learningPoll;
  let failures=0;
  for (let attempt=0; attempt<600 && token===learningPoll; attempt++) {
    let job;
    try {
      const response=await fetch(url,{headers:{Accept:'application/json'},cache:'no-store'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('无法查看任务，请刷新页面或重新登录。');
      job=await response.json(); failures=0;
    } catch (error) {
      if (++failures>=3) throw new Error('进度连接中断，不代表任务失败。请刷新查看最近任务，不会重复调用助手。');
      await learningWait(2000); continue;
    }
    if (token!==learningPoll) return;
    displayLearning(job);
    target.textContent=job.status==='pending' ? (job.progress?.detail || '检索与整理中…') : job.status==='ready' ? '草稿已生成，尚未保存。' : job.error;
    if (job.status!=='pending') {
      if (job.status==='ready') target.append(' ',learningReviewLink(job.review_url));
      return;
    }
    await learningWait(1000);
  }
  if (token===learningPoll) throw new Error('页面等待已结束。请刷新查看最近任务，未自动重复调用助手。');
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
    const view=document.createElement('a'); view.className='text-link'; view.textContent='查看过程'; view.href='/learning?job='+encodeURIComponent(result.job_url.split('/').pop()); row.append(view);
    document.querySelector('[data-learning-empty]')?.remove();
    document.querySelector('[data-learning-jobs]')?.prepend(row);
    history.replaceState(null,'',view.href);
    await pollLearning(result.job_url, target);
  } catch(error) {
    target.textContent=error.message;
    clearInterval(learningTimer); progressPanel?.classList.remove('is-running');
  }
  finally {
    button.disabled=false;
    if (jobStatus) jobStatus.replaceChildren(...Array.from(target.childNodes,node=>node.cloneNode(true)));
  }
});
if (progressPanel?.dataset.jobUrl) {
  pollLearning(progressPanel.dataset.jobUrl, document.querySelector('[data-learning-status]')).catch(error=>{
    document.querySelector('[data-learning-status]').textContent=error.message;
    clearInterval(learningTimer); progressPanel.classList.remove('is-running');
  });
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
