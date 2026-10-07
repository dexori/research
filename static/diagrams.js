'use strict';
// Only fenced Mermaid blocks are rendered; other code and editors are untouched.
(() => {
  let frame, ready, renderQueue=Promise.resolve();
  const waiting=new Map();
  function renderer() {
    if (ready) return ready;
    ready=new Promise((resolve,reject)=>{
      frame=document.createElement('iframe'); frame.className='research-diagram-worker';
      frame.setAttribute('sandbox','allow-scripts'); frame.setAttribute('aria-hidden','true');
      frame.title='隔离图表渲染器'; frame.tabIndex=-1; frame.src='/diagrams/renderer';
      const deadline=setTimeout(()=>reject(new Error('renderer unavailable')),12000);
      const listener=event=>{
        if (event.source!==frame.contentWindow || event.origin!=='null') return;
        const payload=event.data;
        if (payload?.type==='research-diagram-ready') { clearTimeout(deadline); resolve(); }
        if (payload?.type==='research-diagram-result' && typeof payload.id==='string') {
          const job=waiting.get(payload.id);
          if (job) { waiting.delete(payload.id); clearTimeout(job.deadline); payload.error?job.reject(new Error('invalid diagram')):job.resolve(payload.svg); }
        }
      };
      window.addEventListener('message',listener);
      document.body.append(frame);
    });
    return ready;
  }
  async function draw(source) {
    await renderer();
    return new Promise((resolve,reject)=>{
      const id=crypto.randomUUID();
      const deadline=setTimeout(()=>{waiting.delete(id);reject(new Error('diagram timeout'));},15000);
      waiting.set(id,{resolve,reject,deadline});
      frame.contentWindow.postMessage({type:'research-diagram-render',id,source},'*');
    });
  }
  function safeSvg(text) {
    if (typeof text!=='string' || text.length>2000000) throw new Error('invalid SVG');
    const doc=new DOMParser().parseFromString(text,'image/svg+xml');
    if (doc.querySelector('parsererror') || doc.documentElement.localName!=='svg') throw new Error('invalid SVG');
    const allowed=new Set(['svg','g','defs','marker','path','polygon','polyline','line','circle','ellipse','rect','text','tspan','title','desc','clipPath','pattern','linearGradient','radialGradient','stop']);
    const dangerous=value=>/@import|javascript:|https?:|data:|file:/i.test(value) ||
      Array.from(value.matchAll(/url\s*\(([^)]*)\)/gi)).some(([,target])=>!target.trim().replace(/^['"]|['"]$/g,'').startsWith('#'));
    for (const node of Array.from(doc.querySelectorAll('*'))) {
      if (!allowed.has(node.localName)) { node.remove(); continue; }
      for (const attribute of Array.from(node.attributes)) {
        if (/^on/i.test(attribute.name) || attribute.localName==='style' || ((attribute.localName==='href') && !attribute.value.startsWith('#')) ||
            (attribute.localName!=='xmlns' && attribute.name!=='xmlns:xlink' && dangerous(attribute.value))) node.removeAttributeNode(attribute);
      }
    }
    return new XMLSerializer().serializeToString(doc.documentElement);
  }
  function image(svg) {
    const img=document.createElement('img'); img.className='research-diagram-image';
    img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
    img.alt='根据文字整理的结构示意图；可展开源码阅读节点和关系。';
    return img;
  }
  function button(text,action) {
    const node=document.createElement('button'); node.type='button'; node.className='button secondary';
    node.textContent=text; node.addEventListener('click',action); return node;
  }
  function enlarge(svg) {
    const dialog=document.createElement('dialog'); dialog.className='research-diagram-dialog';
    const tools=document.createElement('div'); tools.className='research-diagram-tools';
    const canvas=document.createElement('div'); canvas.className='research-diagram-viewport';
    const img=image(svg); canvas.append(img);
    let scale=1;
    function zoom(value) { scale=Math.max(.5,Math.min(4,value)); img.style.width=(scale*100)+'%'; }
    tools.append(button('缩小',()=>zoom(scale-.25)),button('放大',()=>zoom(scale+.25)),
      button('适应窗口',()=>zoom(1)),button('关闭',()=>dialog.close()));
    dialog.append(tools,canvas); document.body.append(dialog);
    dialog.addEventListener('close',()=>dialog.remove(),{once:true});
    dialog.showModal();
  }
  function download(svg) {
    const url=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}));
    const link=document.createElement('a'); link.href=url; link.download='research-diagram.svg';
    document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  window.renderResearchDiagrams=(root=document)=>{
    const jobs=[];
    root.querySelectorAll('pre > code.language-mermaid').forEach(code=>{
      if (code.dataset.diagramProcessed) return;
      code.dataset.diagramProcessed='true';
      const source=code.textContent, pre=code.parentElement;
      const panel=document.createElement('figure'); panel.className='research-diagram';
      const caption=document.createElement('figcaption'); caption.textContent='结构示意图 · Mermaid';
      const viewport=document.createElement('div'); viewport.className='research-diagram-viewport';
      const status=document.createElement('p'); status.className='small muted'; status.textContent='正在本地渲染结构图…';
      const tools=document.createElement('div'); tools.className='research-diagram-tools';
      const details=document.createElement('details'), summary=document.createElement('summary'); summary.textContent='查看图表源码';
      pre.replaceWith(panel); details.append(summary,pre); panel.append(caption,status,viewport,tools,details);
      const run=async()=>{
        if (!panel.isConnected) return;
        try {
          if (source.length>20000) throw new Error('too large');
          const svg=safeSvg(await draw(source));
          if (!panel.isConnected) return;
          viewport.append(image(svg)); panel.dataset.diagramReady='true'; status.remove();
          tools.append(button('放大查看',()=>enlarge(svg)),button('下载 SVG',()=>download(svg)));
        } catch {
          if (!panel.isConnected) return;
          panel.dataset.diagramError='true'; details.open=true;
          status.textContent='图表暂未渲染：可能有语法错误、不支持的类型或外链/配置指令。源码已保留，不影响正文。';
        }
      };
      renderQueue=renderQueue.then(run,run); jobs.push(renderQueue);
    });
    return Promise.all(jobs);
  };
  window.renderResearchDiagrams();
})();
