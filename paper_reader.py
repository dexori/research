"""Bounded public-paper fetching and text extraction. Never executes page code."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_TEXT = 180000


class PaperError(Exception):
    pass


def public_target(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise PaperError('论文链接无效。')
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in {None, 443}:
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii')
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (ValueError, UnicodeError, OSError):
        raise PaperError('仅支持可解析的公开 HTTPS 论文链接（443 端口）。') from None
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
        raise PaperError('不能读取本机、内网、保留地址或云元数据地址。')
    netloc = '[' + host + ']' if ':' in host else host
    return host, ips[0], urlunsplit(('https', netloc, quote(p.path or '/', safe='/%:@!$&\'()*+,;=-._~'),
                                   quote(p.query, safe='/%?:@!$&\'()*+,;=-._~'), ''))


class PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin the validated public IP; TLS still verifies the original hostname."""
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.public_ip = ip

    def connect(self):
        proxy = os.environ.get('RESEARCH_PAPER_PROXY')
        if proxy:
            p = urlsplit(proxy)
            if p.scheme != 'http' or not p.hostname or p.username or p.password:
                raise PaperError('RESEARCH_PAPER_PROXY 仅支持不含凭据的 HTTP 代理地址。')
            self.set_tunnel(self.public_ip, 443)
            self.sock = socket.create_connection((p.hostname, p.port or 80), self.timeout)
            self._tunnel()
        else:
            self.sock = socket.create_connection((self.public_ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def download(url, deadline):
    for _ in range(5):
        host, ip, url = public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PaperError('读取论文超时，请稍后重试或上传 PDF。')
        connection = PublicHTTPSConnection(host, ip, min(12, remaining))
        try:
            p = urlsplit(url)
            connection.request('GET', p.path + ('?' + p.query if p.query else ''), headers={
                'User-Agent': 'ResearchHub/1.0 (personal paper reader)', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise PaperError('论文站点返回了无效跳转。')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise PaperError(f'论文站点返回 HTTP {response.status}；未读取正文，请上传公开 PDF 或稍后重试。')
            size = response.getheader('Content-Length', '')
            if size.isdigit() and int(size) > MAX_DOWNLOAD:
                raise PaperError('论文文件超过 20 MB 的读取上限。')
            if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
                raise PaperError('论文站点返回了暂不支持的压缩格式，请上传 PDF。')
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PaperError('读取论文超时，请上传 PDF 或重试。')
                if connection.sock:
                    connection.sock.settimeout(min(12, remaining))
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise PaperError('论文文件超过 20 MB 的读取上限。')
                chunks.append(chunk)
            return b''.join(chunks), response.getheader('Content-Type', ''), url
        except (OSError, UnicodeError, http.client.HTTPException):
            raise PaperError('连接论文站点失败。请检查网络，或上传 PDF 后读取。') from None
        finally:
            connection.close()
    raise PaperError('论文链接跳转次数过多，请使用直接 PDF 链接。')


def paper_url(url):
    p = urlsplit(url)
    if p.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
        match = re.fullmatch(r'/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?/?', p.path)
        if match:
            return 'https://arxiv.org/pdf/' + match[1]
    if p.hostname in {'openreview.net', 'www.openreview.net'} and p.path == '/forum':
        identifier = parse_qs(p.query).get('id', [''])[0]
        if identifier:
            return 'https://openreview.net/pdf?' + urlencode({'id': identifier})
    return url


def extract_pdf(raw):
    if len(raw) > MAX_DOWNLOAD or not raw.lstrip().startswith(b'%PDF-'):
        raise PaperError('不是有效 PDF，或超过 20 MB 的读取上限。')
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--pdf-worker'],
                                input=raw, capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise PaperError('PDF 解析超时；请换用文本版 PDF。') from None
    if result.returncode:
        raise PaperError('PDF 无法解析，可能加密、损坏或过于复杂；请换用文本版 PDF。')
    try:
        payload = json.loads(result.stdout)
        if len(payload['text'].strip()) < 200 or payload.get('extracted_chars',0) < 200:
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError):
        raise PaperError('未能提取足够正文：可能是扫描 PDF，目前不支持 OCR。') from None


def pdf_worker():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    except ImportError:
        pass
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(sys.stdin.buffer.read(MAX_DOWNLOAD + 1)))
    if reader.is_encrypted or len(reader.pages) > 300:
        raise ValueError('Encrypted or too many pages')
    parts, chars, used, extracted = [], 0, 0, 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ''
        extracted += len(text.strip())
        part = f'\n\n[PDF 第 {number} 页]\n' + text
        parts.append(part[:MAX_TEXT - chars])
        chars += len(parts[-1]); used = number
        if chars >= MAX_TEXT:
            break
    print(json.dumps({'text': ''.join(parts), 'pages': len(reader.pages),
                      'pages_read': used, 'truncated': chars >= MAX_TEXT,
                      'extracted_chars': extracted, 'method': 'PDF 文本提取（不含图片识别）'}, ensure_ascii=False))


class PaperReader:
    def __init__(self):
        self.gate = threading.Lock()

    def read(self, url=None, raw=None):
        if not self.gate.acquire(blocking=False):
            raise PaperError('已有论文正在读取，请稍后重试。')
        try:
            if raw is not None:
                return extract_pdf(raw)
            deadline = time.monotonic() + 35
            target = paper_url(url)
            for attempt in range(2):
                body, mime, final = download(target, deadline)
                if body.lstrip().startswith(b'%PDF-'):
                    return {**extract_pdf(body), 'final_url': final}
                if 'html' not in mime:
                    raise PaperError('链接没有返回 PDF 或论文 HTML 正文。')
                soup = BeautifulSoup(body, 'html.parser')
                citation = soup.find('meta', attrs={'name': re.compile('^citation_pdf_url$', re.I)})
                if attempt == 0 and citation and citation.get('content'):
                    target = urljoin(final, citation['content'])
                    continue
                article = soup.find('article')
                if not article or len(article.find_all(re.compile('^h[1-6]$'))) < 2:
                    raise PaperError('该链接只提供摘要或网页信息，未确认论文正文；请使用直接 PDF 链接或上传 PDF。')
                for tag in article(['script', 'style', 'nav', 'form', 'footer', 'noscript']):
                    tag.decompose()
                text = article.get_text('\n', strip=True)
                if len(text) < 2000:
                    raise PaperError('网页正文过短，不能确认已读取论文；请上传 PDF。')
                return {'text': text[:MAX_TEXT], 'method': '论文 HTML 文本提取', 'final_url': final,
                        'truncated': len(text) > MAX_TEXT, 'pages': None, 'pages_read': None}
            raise PaperError('未找到可读取的论文正文。')
        finally:
            self.gate.release()


if __name__ == '__main__' and sys.argv[1:] == ['--pdf-worker']:
    pdf_worker()
