"""Protect pasted TeX at display time, without rewriting the stored Markdown."""
from html import escape
import re

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor


TOKENS = re.compile(
    r'(?P<ticks>`+)|'
    r'(?P<html><(?P<tag>pre|code|script|style|textarea)\b[^>]*>)|'
    r'(?P<math>(?<!\\)(?P<slashes>\\{1,2})(?P<delimiter>[\[(]))|'
    r'(?P<dollars>(?<![\\$])\$\$(?!\$))', re.IGNORECASE)


class PastedMathPreprocessor(Preprocessor):
    def run(self, lines):
        # Fenced code and raw block HTML have already been stashed by Markdown.
        text = '\n'.join(lines)
        parts, cursor, protected_until = [], 0, 0
        for token in TOKENS.finditer(text):
            start, end = token.span()
            if start < protected_until:
                continue
            line_start = text.rfind('\n',0,start)+1
            if re.match(r'(?: {4}|\t)',text[line_start:start]):
                # Indented Markdown code is parsed later; leave it untouched.
                protected_until = text.find('\n',end)
                if protected_until < 0:
                    protected_until = len(text)
                continue
            if token.group('ticks'):
                boundary = text.find('\n\n',end)
                close = re.compile(r'(?<!`)`{'+str(end-start)+r'}(?!`)').search(
                    text,end,len(text) if boundary<0 else boundary)
                if close:
                    protected_until = close.end()
                continue
            if token.group('html'):
                close = re.search(r'</'+token.group('tag')+r'\s*>',text[end:],re.IGNORECASE)
                protected_until = end+close.end() if close else len(text)
                continue
            delimiter = token.group('delimiter')
            display = delimiter=='[' or bool(token.group('dollars'))
            if token.group('dollars'):
                closing = re.compile(r'(?<![\\$])\$\$(?!\$)')
            else:
                closing = re.compile(r'(?<!\\)'+re.escape(token.group('slashes')+(']' if display else ')')))
            limit = min(len(text),end+20000)
            if not display:
                boundary = text.find('\n\n',end,limit)
                if boundary>=0:
                    limit=boundary
            close = closing.search(text,end,limit)
            if close is None:
                continue
            body = text[end:close.start()]
            if not body.strip():
                continue
            # Only delimiters are normalized. TeX commands and matrix/cases row
            # separators stay byte-for-byte identical in the displayed source.
            opening, ending = ('\\[','\\]') if display else ('\\(','\\)')
            tag = 'div' if display else 'span'
            html = '<'+tag+' class="arithmatex">'+escape(opening+body+ending)+'</'+tag+'>'
            placeholder = self.md.htmlStash.store(html)
            parts.append(text[cursor:start])
            parts.append('\n\n'+placeholder+'\n\n' if display else placeholder)
            cursor = protected_until = close.end()
        parts.append(text[cursor:])
        return ''.join(parts).split('\n')


class MathCompatExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(PastedMathPreprocessor(md),'pasted_math',15)
