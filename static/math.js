'use strict';
// Only Markdown-protected math nodes, never code blocks or the editor textarea.
window.renderResearchMath = (root = document) => {
  if (!window.katex) return;
  root.querySelectorAll('.arithmatex:not([data-math-rendered])').forEach(node => {
    if (node.closest('pre,code,textarea')) return;
    const source = node.textContent;
    const display = source.startsWith('\\[') && source.endsWith('\\]');
    const inline = source.startsWith('\\(') && source.endsWith('\\)');
    if (!display && !inline) return;
    node.dataset.mathRendered = 'true';
    node.dataset.mathSource = source;
    if (source.length > 20000) {
      node.classList.add('math-fallback');
      node.title = '公式过长，保留原文';
      return;
    }
    try {
      window.katex.render(source.slice(2,-2), node, {
        displayMode:display, output:'htmlAndMathml', trust:false,
        throwOnError:true, strict:'ignore', maxExpand:1000, maxSize:20,
        macros:{} // A fresh scope for every formula; no cross-message macro persistence.
      });
      node.classList.add(display ? 'math-display' : 'math-inline');
    } catch (error) {
      node.textContent = source;
      node.classList.add('math-fallback');
      node.title = '公式语法暂不支持或有误，已保留原文';
    }
  });
};
window.renderResearchMath();
