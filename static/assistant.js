'use strict';
const chat = document.querySelector('[data-assistant]');
if (chat) {
  const form = chat.querySelector('[data-chat-form]');
  const input = form.querySelector('[name=message]');
  const button = form.querySelector('[data-chat-send]');
  const status = form.querySelector('[data-chat-status]');
  const container = chat.querySelector('[data-chat-messages]');
  const project = chat.querySelector('#chat-project');
  let busy = false;
  let timer;
  const showMessages = messages => {
    container.replaceChildren();
    for (const item of messages) {
      const article = document.createElement('article');
      article.className = `chat-message ${item.role === 'user' ? 'user' : 'assistant'} ${item.status}`;
      article.dataset.messageId = item.id;
      const label = document.createElement('div');
      label.className = 'chat-message-label';
      label.textContent = item.role === 'user' ? '我' : `Codex · 科研助手${item.status === 'error' ? ' · 未完成' : ''}`;
      const body = document.createElement('div');
      body.className = 'chat-message-body';
      if (item.status === 'pending') body.textContent = 'Codex 正在思考，请稍候…';
      else body.innerHTML = item.html; // HTML is sanitized by the authenticated backend.
      article.append(label,body);
      if (item.role === 'assistant' && item.status === 'complete') {
        const sources = document.createElement('div'); sources.className = 'chat-sources';
        const draft = document.querySelector('#chat-draft-template').content.cloneNode(true);
        draft.querySelector('[name=message_id]').value = item.id;
        const targets = draft.querySelector('[name=target_id]');
        for (const source of item.sources) {
          if (!/^[a-f0-9]{32}$/.test(source.id)) continue;
          const link = document.createElement('a'); link.href = `/entry/${source.id}`;
          link.textContent = `▤ ${source.title}` + (source.paper_chars !== undefined ? ` · 原文 ${source.paper_chars.toLocaleString()} 字符${source.paper_truncated ? '（截断）' : ''}` : ''); link.target = '_blank'; link.rel = 'noopener';
          sources.append(link);
          const option = document.createElement('option'); option.value=source.id;
          option.textContent=`追加到：${source.title}`;
          if (source.id === form.querySelector('[name=context_id]').value && draft.querySelector('[name=kind]').value === 'paper') option.selected=true;
          targets.append(option);
        }
        article.append(sources,draft);
      }
      container.append(article);
    }
    window.renderResearchMath?.(container);
    container.scrollTop = container.scrollHeight;
    busy = messages.some(item => item.status === 'pending');
    button.disabled = busy;
    button.textContent = busy ? 'Codex 思考中…' : '发送给 Codex';
    status.textContent = busy ? '正在调用本机 Codex，最多等待 3 分钟；可以在其他页面继续工作。' : '回答不会自动写入笔记，保存前请核对事实与引用。';
  };
  const setThread = identifier => {
    chat.dataset.threadId = identifier;
    form.querySelector('[name=thread_id]').value = identifier;
    project.disabled=true;
    const url = new URL(window.location.href); url.searchParams.set('thread',identifier); url.searchParams.delete('entry');
    window.history.replaceState(null,'',url);
  };
  const decode = async response => {
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('页面会话已过期，请刷新并登录后重试。');
    return response.json();
  };
  const poll = async () => {
    const identifier = chat.dataset.threadId;
    if (!identifier) return;
    try {
      const url = chat.dataset.threadPrefix.replace('THREAD_ID',identifier);
      const payload = await decode(await fetch(url,{cache:'no-store'}));
      if (!payload.messages) throw new Error(payload.error || '无法读取对话，请刷新页面。');
      showMessages(payload.messages);
      if (busy) timer=setTimeout(poll,3000);
    } catch (error) { status.textContent=error.message; }
  };
  form.addEventListener('submit',async event => {
    event.preventDefault();
    if (busy || !input.value.trim()) return;
    busy=true;button.disabled=true;button.textContent='Codex 思考中…';
    status.textContent='正在检索相关笔记并调用本机 Codex，请稍候…';
    const data=new FormData(form);data.set('project_id',project.value);
    try {
      const response=await fetch(chat.dataset.sendUrl,{method:'POST',body:data});
      const payload=await decode(response);
      if (payload.thread_id) setThread(payload.thread_id);
      if (payload.messages) { showMessages(payload.messages); input.value=''; }
      if (payload.paper_status) {
        document.querySelectorAll('[data-paper-read-status],[data-paper-source-status]').forEach(node=>node.textContent=payload.paper_status);
      }
      if (payload.error) status.textContent=payload.error;
      if (!response.ok && !payload.messages) throw new Error(payload.error || '发送失败，请稍后重试。');
    } catch(error) { status.textContent=error.message; }
    finally { busy=false;button.disabled=false;button.textContent='发送给 Codex'; }
  });
  input.addEventListener('keydown', event => {
    if (event.key==='Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault();form.requestSubmit(); }
  });
  document.querySelectorAll('[data-chat-prompt]').forEach(item => item.addEventListener('click',() => {input.value=item.dataset.chatPrompt;input.focus();}));
  if (container.querySelector('.pending')) timer=setTimeout(poll,1500);
}

document.querySelectorAll('[data-codex-check]').forEach(form=>form.addEventListener('submit', async event=>{
  event.preventDefault();
  const button=form.querySelector('button'), status=form.querySelector('[data-codex-status]');
  button.disabled=true;status.textContent='正在检查本机 CLI 登录…';
  try {
    const response=await fetch(form.action,{method:'POST',body:new FormData(form)});
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('检查未完成，请刷新并登录平台后重试。');
    const data=await response.json();status.textContent=data.message + (data.profile ? ` 配置目录：${data.profile}` : '');
  } catch(error) {status.textContent=error.message;}
  finally {button.disabled=false;}
}));
document.querySelectorAll('[data-paper-fetch]').forEach(form=>form.addEventListener('submit',()=>{
  const button=form.querySelector('button');button.disabled=true;button.textContent='正在读取，请稍候…';
}));
