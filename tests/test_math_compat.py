import markdown
import unittest

from math_compat import MathCompatExtension


def render(text,compat=True):
    extensions=['fenced_code','tables','sane_lists','pymdownx.arithmatex']
    if compat:
        extensions.append(MathCompatExtension())
    return markdown.markdown(text,extensions=extensions,
                             extension_configs={'pymdownx.arithmatex':{'generic':True}})


class MathCompatTest(unittest.TestCase):
    def test_legacy_inline_and_blocks_attached_to_prose(self):
        text = r'''窗口 \\(H\\)，表示 \\(\widetilde A\\)。
使用 DTW 得到：
\[
d_{ij}^{H}=\operatorname{DTW}(A_i,A_j)
\]
下一段正文。'''
        original=text
        html=render(text)
        self.assertEqual(html.count('class="arithmatex"'),3)
        self.assertIn(r'\(H\)',html)
        self.assertIn(r'\widetilde A',html)
        self.assertIn(r'\operatorname{DTW}',html)
        self.assertIn('下一段正文',html)
        self.assertEqual(text,original)

    def test_same_line_and_dollars_display_blocks(self):
        html=render(r'之前\[x < y\]之间$$a=b$$之后')
        self.assertEqual(html.count('<div class="arithmatex">'),2)
        self.assertIn('x &lt; y',html)
        self.assertLess(html.index('之前'),html.index('x &lt; y'))
        self.assertLess(html.index('a=b'),html.index('之后'))

    def test_cases_blank_lines_and_matrix_row_separators_preserved(self):
        body=r'''w=\begin{cases}
1-F_H(d_{ij}^H), & \text{有效正样本}\\

0, & \text{其他}
\end{cases}'''
        html=render('先计算：\n\\[\n'+body+'\n\\]\n再归一化。')
        self.assertEqual(html.count('class="arithmatex"'),1)
        self.assertIn(r'\text{有效正样本}\\',html)
        self.assertIn(r'\end{cases}',html)
        self.assertIn('再归一化',html)

    def test_inline_fenced_indented_and_html_code_stay_literal(self):
        samples=[r'`\\(H\\)`',r'``text ` \[x\]``',
                 '```text\n\\[\nx=y\n\\]\n```',
                 '~~~text\n$$a=b$$\n~~~',
                 '\n    \\[x\\]\n    \\(H\\)\n',
                 r'<code>\\(H\\) \[x\]</code>',
                 r'<pre>\[x\]</pre>']
        for text in samples:
            with self.subTest(text=text):
                html=render(text)
                self.assertNotIn('arithmatex',html)
                self.assertEqual(html,render(text,compat=False))

    def test_code_then_real_math_and_unmatched_delimiters(self):
        html=render(r'`\\(code\\)` 后面 \\(H\\)。')
        self.assertEqual(html.count('class="arithmatex"'),1)
        self.assertIn(r'<code>\\(code\\)</code>',html)
        for text in [r'不完整 \[x',r'不完整 \\(x',r'普通 \\\(x\\\)',r'转义美元 \$$5']:
            self.assertEqual(render(text),render(text,compat=False))

    def test_generated_math_escapes_html(self):
        html=render(r'\[x < y & z > w\]')
        self.assertIn('x &lt; y &amp; z &gt; w',html)
        self.assertNotIn('x < y',html)


if __name__=='__main__':
    unittest.main()
