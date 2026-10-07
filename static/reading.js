'use strict';
(() => {
  let serial=0;
  const enhance=root=>{
    const readers=[];
    if (root.matches?.('.research-reading,.import-preview .prose,.chat-message-body')) readers.push(root);
    readers.push(...root.querySelectorAll('.research-reading,.import-preview .prose,.chat-message-body'));
    for (const reader of readers) {
      if (reader.dataset.readingReady) continue;
      reader.dataset.readingReady='1';reader.classList.add('research-reading');
      const headings=Array.from(reader.children).filter(node=>node.tagName==='H2');
      if (headings.length>=3) {
        const toc=document.createElement('details');toc.className='reading-toc';toc.open=true;
        const title=document.createElement('summary');title.textContent='阅读目录';
        const links=document.createElement('nav');links.setAttribute('aria-label','正文目录');
        for (const heading of headings) {
          heading.id=`research-section-${++serial}`;
          const link=document.createElement('a');link.href='#'+heading.id;link.textContent=heading.textContent;links.append(link);
        }
        toc.append(title,links);reader.prepend(toc);
      }
      for (const heading of headings) {
        const section=document.createElement('section');section.className='reading-section';
        const title=heading.textContent;
        if (/一句话|摘要|总结/.test(title)) section.classList.add('reading-summary');
        if (/核心|原理|最小例子|训练与推理/.test(title)) section.classList.add('reading-key');
        if (/误区|局限|待确认/.test(title)) section.classList.add('reading-caution');
        if (/来源|整理记录/.test(title)) section.classList.add('reading-reference');
        heading.before(section);section.append(heading);
        while (section.nextSibling && section.nextSibling.nodeName!=='H2') section.append(section.nextSibling);
      }
      for (const img of reader.querySelectorAll('img')) {
        if (!/^\/(media|draft-media)\/[a-f0-9]{32}$/.test(img.getAttribute('src') || '')) continue;
        img.classList.add('reading-image');img.tabIndex=0;img.setAttribute('role','button');
        img.setAttribute('aria-label',(img.alt || '科研插图')+'，点击放大');
        if (img.closest('figure')) continue;
        const figure=document.createElement('figure');figure.className='reading-figure';
        const paragraph=img.parentElement;
        if (paragraph.tagName==='P' && paragraph.children.length===1 && paragraph.textContent.trim()==='') {
          const caption=paragraph.nextElementSibling;
          paragraph.replaceWith(figure);figure.append(img);
          if (caption?.tagName==='P' && caption.children.length===1 && caption.firstElementChild.tagName==='EM' && /^图\s*\d/.test(caption.textContent)) {
            const label=document.createElement('figcaption');label.textContent=caption.textContent;caption.remove();figure.append(label);
          }
        } else {img.before(figure);figure.append(img);}
      }
    }
  };
  window.renderResearchReading=enhance;
  enhance(document);
  const openImage=img=>{
    const dialog=document.createElement('dialog');dialog.className='image-viewer';
    const heading=document.createElement('h2');heading.textContent=img.alt || '科研插图';
    const image=document.createElement('img');image.src=img.getAttribute('src');image.alt=img.alt;
    const close=document.createElement('button');close.type='button';close.className='button secondary';close.textContent='关闭';
    const download=document.createElement('a');download.href=image.src;download.download='research-illustration';download.className='button primary';download.textContent='下载图片';
    const footer=document.createElement('div');footer.className='heading-actions';footer.append(close,download);
    dialog.append(heading,image,footer);document.body.append(dialog);
    close.addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>{dialog.remove();img.focus();});dialog.showModal();
  };
  document.addEventListener('click',event=>{const img=event.target.closest('.reading-image');if(img)openImage(img);});
  document.addEventListener('keydown',event=>{if(event.target.matches('.reading-image') && ['Enter',' '].includes(event.key)){event.preventDefault();openImage(event.target);}});

  const editor=document.querySelector('[data-editor]'), content=editor?.querySelector('[name=content]');
  if (editor && content) {
    const preview=editor.querySelector('[data-editor-preview]'), state=editor.querySelector('[data-preview-status]');
    let timer, sequence=0;
    content.addEventListener('input',()=>{
      clearTimeout(timer);const token=++sequence;
      timer=setTimeout(async()=>{
        const data=new FormData();data.set('csrf',editor.elements.csrf.value);data.set('content',content.value);
        try {
          const response=await fetch(editor.dataset.previewUrl,{method:'POST',body:data});
          if(!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error();
          const result=await response.json();if(token!==sequence)return;
          preview.innerHTML=result.html;delete preview.dataset.readingReady;
          window.renderResearchMath?.(preview);window.renderResearchDiagrams?.(preview);enhance(preview);
          state.textContent='预览已更新 · 未保存';
        } catch {if(token===sequence)state.textContent='预览暂未更新，请检查登录；输入内容仍保留在编辑框。';}
      },450);
    });
    const button=editor.querySelector('[data-illustrate-url]'), status=editor.querySelector('[data-illustrate-status]');
    button?.addEventListener('click',async()=>{
      button.disabled=true;status.textContent='正在提交本次编辑正文，生成一张 img2 配图…';
      try {
        const response=await fetch(button.dataset.illustrateUrl,{method:'POST',body:new FormData(editor)});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('请刷新确认平台登录。');
        const result=await response.json();if(!response.ok)throw new Error(result.error || '配图任务未启动。');
        for(let count=0;count<900;count++) {
          const reply=await fetch(result.job_url,{cache:'no-store'});
          if(!reply.ok || !reply.headers.get('content-type')?.includes('application/json'))throw new Error('无法读取进度，请在学习整理页查看任务。');
          const job=await reply.json();status.textContent=job.progress.detail+' · '+job.elapsed_seconds+' 秒';
          if(job.status==='ready') {
            const link=document.createElement('a');link.className='button primary';link.href=job.review_url;link.textContent='打开新版本，审核图文';status.append(' ',link);break;
          }
          if(job.status==='error')throw new Error(job.error);
          await new Promise(resolve=>setTimeout(resolve,1000));
        }
      } catch(error){status.textContent=error.message;}
      finally{button.disabled=false;}
    });
  }
})();
