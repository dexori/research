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
