"""Validate sourced learning-card drafts; no automatic canonical-note writes."""
from datetime import datetime, timezone
import ipaddress
import re
from urllib.parse import urlsplit
from urllib.parse import urlencode
import json
import threading
import time
import xml.etree.ElementTree as ET

from codex_bridge import BridgeError
from paper_reader import PaperReader, PaperError, download

CATEGORIES = ['模型架构', '训练方法', '数学基础', '工程实践', '其他']

ALIASES = [
    (r'(?:π|pi|phi)[ _-]?0[.]5', 'ti:"A Vision-Language-Action Model with Open-World Generalization"'),
    (r'(?:π|pi|phi)[ _-]?0(?:\b|架构|模型)', 'ti:"A Vision-Language-Action Flow Model for General Robot Control"'),
    (r'(?<![A-Za-z0-9])act(?![A-Za-z0-9])', 'ti:"Learning Fine-Grained Bimanual Manipulation with Low-Cost Hardware"'),
    (r'flow[ -]matching|流匹配', 'ti:"Flow Matching for Generative Modeling"'),
]


class PublicLearningSearch:
    """Search the fixed arXiv endpoint, not model-invented URLs or private notes."""
    def __init__(self):
        self.gate = threading.Lock()
        self.last_request = 0

    def query(self, topic, bridge):
        for pattern, query in ALIASES:
            if re.search(pattern, topic, re.I):
                return query
        raw = bridge.answer('将下列学习主题转换为 arXiv 文献检索用英文关键词。只输出 JSON，格式 '
                            '{"keywords":["短语1","短语2"]}，1–3 个短语，每项最多 60 字符。不要回答知识内容，'
                            '不要给链接，歧义时采用科研/机器学习语境。主题：'+topic, [], [], timeout=90)
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        try:
            keywords = json.loads(raw)['keywords']
            if not isinstance(keywords, list) or not 1<=len(keywords)<=3:
                raise ValueError()
            if any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}',k) for k in keywords):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise BridgeError('无法提取可靠的英文检索关键词，请补充英文术语后重试。') from None
        return ' AND '.join('all:"'+word+'"' for word in keywords)

    def search(self, query):
        # API guidance asks clients to space requests at least three seconds apart.
        with self.gate:
            delay = 3-(time.monotonic()-self.last_request)
            if delay>0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            url = 'https://export.arxiv.org/api/query?'+urlencode({'search_query':query,'max_results':3,'sortBy':'relevance'})
            raw, _, _ = download(url, time.monotonic()+25)
        if len(raw)>1_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise BridgeError('文献检索响应无效或过大。')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise BridgeError('文献检索没有返回有效的 arXiv 结果。') from None
        ns = {'a':'http://www.w3.org/2005/Atom'}
        sources = []
        for entry in root.findall('a:entry',ns)[:3]:
            identifier = entry.findtext('a:id',default='',namespaces=ns)
            match = re.fullmatch(r'https?://(?:export\.)?arxiv.org/abs/(\d{4}\.\d{4,5}(?:v\d+)?|[A-Za-z.-]+/\d{7}(?:v\d+)?)', identifier)
            if not match:
                continue
            sources.append({'url':'https://arxiv.org/abs/'+match[1],
                            'title':' '.join(entry.findtext('a:title',default='',namespaces=ns).split())[:500],
                            'summary':entry.findtext('a:summary',default='',namespaces=ns)[:12000],
                            'published':entry.findtext('a:published',default='',namespaces=ns)[:50]})
        if not sources:
            raise BridgeError('arXiv 未找到可读取的论文，请补充英文主题或改用 Codex 网页搜索。')
        return sources

    def research(self, topic, bridge):
        try:
            query = self.query(topic,bridge)
            candidates = self.search(query)
            sources, budget = [], 65000
            reader = PaperReader()
            for candidate in candidates[:2]:
                try:
                    result = reader.read(url=candidate['url'])
                    text = result['text'][:min(budget,40000)]
                    budget -= len(text)
                    reading = {k:result[k] for k in ['method','pages','pages_read','truncated'] if k in result}
                    reading['context_truncated'] = len(text)<len(result['text'])
                    reading['sent_chars'] = len(text)
                    sources.append({**candidate,'content':text,'reading':reading})
                except PaperError:
                    # Explicitly identify metadata-only sources; never call an abstract full text.
                    sources.append({**candidate,'content':candidate['summary'],'reading':{'method':'arXiv 摘要，正文读取失败','abstract_only':True}})
            if not any(not source['reading'].get('abstract_only') for source in sources):
                raise BridgeError('已检索到论文，但正文读取失败；未生成知识卡。请检查论文网络或改用 Codex 网页搜索。')
            content = bridge.answer(topic, [], sources, timeout=240, sourced_learning=True)
            receipt = '\n\n## 平台实际检索来源\n\n'
            for source in sources:
                scope = source['reading']['method']
                if source['reading'].get('context_truncated') or source['reading'].get('truncated'):
                    scope += '；发送给助手的正文有截断'
                receipt += f"- [{source['title']}]({source['url']}) · {scope}\n"
            return {'content':content+receipt,'search_count':1,'search_method':'平台 arXiv 文献检索 + 正文提取',
                    'source_urls':[s['url'] for s in sources]}
        except PaperError as error:
            raise BridgeError('公开文献检索未完成：'+str(error)) from None


def prepare_card(topic, result):
    content = result.get('content', '').strip()
    count = result.get('search_count', 0)
    if type(count) is not int or count < 1 or not content or len(content) > 60000:
        raise ValueError('没有有效的联网检索与知识卡内容；未保存知识卡。')
    links = []
    for url in re.findall(r'\]\((https://[^\s<>]+)\)', content):
        try:
            p = urlsplit(url)
            if not p.hostname or p.username or p.password or p.port not in {None,443}:
                continue
            host=p.hostname.lower()
            if '.' not in host or host.endswith(('.local','.localhost','.internal')):
                continue
            try:
                address=ipaddress.ip_address(host)
                if not address.is_global or address.is_multicast:
                    continue
            except ValueError:
                pass
        except ValueError:
            continue
        if url not in links:
            links.append(url)
    if not links:
        raise ValueError('没有可核验的公开 HTTPS 来源链接；请缩小主题后重试。')
    category_match=re.search(r'^\s*[*_]*分类\s*[：:]\s*[*_]*(.+)',content,re.MULTILINE)
    category='其他'
    if category_match:
        category=next((item for item in CATEGORIES if category_match[1].strip().startswith(item)), '其他')
    tag_match=re.search(r'^\s*[*_]*标签\s*[：:]\s*[*_]*(.+)',content,re.MULTILINE)
    tags=[]
    if tag_match:
        tags=[re.sub(r'[*_`#]','',tag).strip()[:40] for tag in re.split('[,，、]',tag_match[1])]
        tags=[tag for tag in tags if tag][:6]
    date=datetime.now(timezone.utc).isoformat(timespec='seconds')
    method=result.get('search_method','Codex CLI 网页搜索')
    if 'source_urls' in result and any(url not in result['source_urls'] for url in links):
        raise ValueError('草稿引用了平台未读取的额外链接，未保存；请重试并核对来源。')
    receipt=f'\n\n---\n\n## 整理记录\n\n- 学习主题：{topic}\n- 检索时间：{date}\n- 检索方式：{method}\n- 联网检索记录：{count} 次\n- 草稿引用链接：{len(links)} 条\n- 状态：AI 学习草稿，待人工核验。检索记录不代表每条来源均已阅读全文。\n'
    if len(links)<2:
        receipt+='- 来源不足两条，请补充交叉验证。\n'
    return {'content':content+receipt,'category':category,
            'tags':', '.join(dict.fromkeys([category,*tags,'AI 草稿'])),
            'metadata':{'knowledge_category':category,'learning_topic':topic,'search_date':date,'search_count':str(count),'search_method':method}}
