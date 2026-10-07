'use strict';
// Runs only in an opaque-origin iframe; no parent DOM, network, or file access.
const diagramOrigin = new URL(window.location.href).origin;
const diagramCanvas = document.querySelector('#diagram-canvas');
let rendererQueue = Promise.resolve();
function diagramReply(payload) { window.parent.postMessage(payload, diagramOrigin); }
window.addEventListener('message', event => {
  if (event.source!==window.parent || event.origin!==diagramOrigin) return;
  const request=event.data;
  if (!request || request.type!=='research-diagram-render' || typeof request.id!=='string' || !/^[a-f0-9-]{36}$/.test(request.id)) return;
  rendererQueue=rendererQueue.then(async()=>{
    const source=request.source;
    try {
      if (typeof source!=='string' || source.length>20000 || source.split('\n').length>400 ||
          /%%\s*\{|^\s*---|\bclick\s|https?:\/\/|javascript:|data:|file:|@import|url\s*\(|\b(?:img|icon)\s*:/im.test(source)) throw new Error('restricted diagram');
      mermaid.initialize({startOnLoad:false,securityLevel:'strict',htmlLabels:false,
        theme:'base',fontFamily:'Arial, sans-serif',logLevel:5,maxTextSize:20000,maxEdges:120,
        suppressErrorRendering:true,flowchart:{htmlLabels:false,curve:'basis'},
        themeVariables:{primaryColor:'#edf5f1',primaryTextColor:'#20333a',primaryBorderColor:'#6d9b8d',
          lineColor:'#50776a',secondaryColor:'#f3f6fa',tertiaryColor:'#fff7eb'}});
      const result=await mermaid.render('diagram-'+request.id,source,diagramCanvas);
      // Freeze paint styles inside the sandbox so the main page can retain its
      // strict CSP: exported/displayed SVGs contain no inline stylesheet blocks.
      diagramCanvas.innerHTML=result.svg;
      const svg=diagramCanvas.querySelector('svg');
      if (!svg) throw new Error('missing SVG');
      const properties=['fill','fill-opacity','stroke','stroke-opacity','stroke-width','stroke-dasharray',
        'stroke-linecap','stroke-linejoin','opacity','color','font-family','font-size','font-weight',
        'font-style','text-anchor','dominant-baseline','display','visibility','marker-start','marker-mid','marker-end'];
      for (const node of [svg,...svg.querySelectorAll('*')]) {
        if (node.localName==='style') continue;
        const computed=getComputedStyle(node);
        for (const property of properties) {
          let value=computed.getPropertyValue(property);
          if (value.includes('url(')) {
            value=value.replace(/url\(([^)]*)\)/g,(_,target)=>{
              const fragment=target.trim().replace(/^['"]|['"]$/g,'').split('#')[1];
              return fragment && /^[A-Za-z0-9_.:-]+$/.test(fragment)?'url(#'+fragment+')':'none';
            });
          }
          if (value) node.setAttribute(property,value);
        }
        node.removeAttribute('style');
      }
      svg.querySelectorAll('style').forEach(node=>node.remove());
      diagramReply({type:'research-diagram-result',id:request.id,svg:new XMLSerializer().serializeToString(svg)});
    } catch {
      diagramReply({type:'research-diagram-result',id:request.id,error:true});
    } finally { diagramCanvas.replaceChildren(); }
  });
});
diagramReply({type:'research-diagram-ready'});
