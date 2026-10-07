"""Safe Markdown/TeX/image rendering. No database, model calls or file writes."""
import re

import bleach
import markdown
from bs4 import BeautifulSoup
from markupsafe import Markup

from math_compat import MathCompatExtension
from research_images import PORTABLE_IMAGE


def render_md(text):
    html = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists", "pymdownx.arithmatex", MathCompatExtension()],
                             extension_configs={"pymdownx.arithmatex":{"generic":True}})
    # Git-portable relative image references are resolved only to local authenticated media.
    document = BeautifulSoup(html, 'html.parser')
    for image in document.find_all('img'):
        source = str(image.get('src',''))
        match = PORTABLE_IMAGE.fullmatch(source)
        if match:
            image['src'] = '/media/'+match[1]
        elif not re.fullmatch(r'/(?:media|draft-media)/[a-f0-9]{32}',source):
            image.decompose()
            continue
        image['loading'] = 'lazy'
    html = str(document)
    def safe_attribute(tag, name, value):
        if tag=='img':
            return (name in {'alt','title','loading'} or name=='src' and
                    bool(re.fullmatch(r'/(?:media|draft-media)/[a-f0-9]{32}',value)))
        if tag=='a':
            return name in {'href','title'}
        if tag=='code':
            return name=='class'
        return tag in {'span','div'} and name=='class' and value=='arithmatex'
    return Markup(bleach.clean(html, tags={"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "em", "del", "blockquote", "ul", "ol", "li", "code", "pre", "table", "thead", "tbody", "tr", "th", "td", "span", "div", "img"},
                             attributes=safe_attribute, protocols=["http", "https", "mailto"], strip=True))
