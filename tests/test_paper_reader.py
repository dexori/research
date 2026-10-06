import io
import socket
import time
import unittest
from unittest.mock import patch, MagicMock

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from paper_reader import PaperError, PaperReader, public_target, paper_url, download, extract_pdf, MAX_DOWNLOAD


def pdf_bytes(encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 40 700 Td (' + b'Paper methods evidence limitations. ' * 12 + b') Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('secret')
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


class PaperReaderTest(unittest.TestCase):
    def test_pdf_real_extraction_and_failures(self):
        result = extract_pdf(pdf_bytes())
        self.assertIn('methods evidence', result['text'])
        self.assertEqual(result['pages'], 1)
        for raw in [b'not a pdf', b'%PDF-invalid', pdf_bytes(encrypted=True), b'%PDF-'+b'a'*MAX_DOWNLOAD]:
            with self.subTest(size=len(raw)), self.assertRaises(PaperError):
                extract_pdf(raw)
        writer=PdfWriter()
        for _ in range(20): writer.add_blank_page(width=600,height=800)
        output=io.BytesIO();writer.write(output)
        with self.assertRaisesRegex(PaperError,'OCR'):
            extract_pdf(output.getvalue())

    def test_known_landing_links_and_versions(self):
        self.assertEqual(paper_url('https://arxiv.org/abs/2501.09747v2'), 'https://arxiv.org/pdf/2501.09747v2')
        self.assertEqual(paper_url('https://arxiv.org/pdf/cs/0101001.pdf'), 'https://arxiv.org/pdf/cs/0101001')
        self.assertEqual(paper_url('https://openreview.net/forum?id=ABC-def'), 'https://openreview.net/pdf?id=ABC-def')
        self.assertEqual(paper_url('https://example.org/paper.pdf'), 'https://example.org/paper.pdf')

    def test_reject_private_and_mixed_dns_and_bad_urls(self):
        for ip in ['127.0.0.1', '10.0.0.2', '169.254.169.254', '::1', '::ffff:127.0.0.1', '100.64.0.1', '224.0.0.1']:
            with patch('paper_reader.socket.getaddrinfo', return_value=[(socket.AF_INET,1,6,'',(ip,443))]):
                with self.subTest(ip=ip), self.assertRaises(PaperError):
                    public_target('https://example.org/paper')
        with patch('paper_reader.socket.getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(PaperError):
                public_target('https://example.org/')
        for url in ['http://example.org/', 'file:///etc/passwd', 'https://user:pass@example.org/', 'https://example.org:8765/', 'https://example.org/\nheader', None]:
            with self.subTest(url=url), self.assertRaises(PaperError):
                public_target(url)

    def test_public_ip_is_returned_for_pinned_connection(self):
        with patch('paper_reader.socket.getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443))]):
            host, ip, url = public_target('https://example.org/paper.pdf#page=2')
        self.assertEqual((host,ip,url), ('example.org','8.8.8.8','https://example.org/paper.pdf'))

    def test_redirect_to_private_is_rejected_before_second_connection(self):
        response=MagicMock(status=302)
        response.getheader.side_effect=lambda name,default=None: 'https://127.0.0.1/private' if name=='Location' else default
        with patch('paper_reader.socket.getaddrinfo', side_effect=[[(2,1,6,'',('8.8.8.8',443))],[(2,1,6,'',('127.0.0.1',443))]]), patch('paper_reader.PublicHTTPSConnection') as connection:
            connection.return_value.getresponse.return_value=response
            with self.assertRaises(PaperError): download('https://example.org/', time.monotonic()+5)
            self.assertEqual(connection.call_count,1)
            connection.return_value.close.assert_called_once()

    def test_landing_metadata_follows_pdf_but_abstract_is_not_full_text(self):
        landing=b'<html><meta name="citation_pdf_url" content="/paper.pdf"><h1>Paper</h1><p>Abstract only.</p></html>'
        with patch('paper_reader.download', side_effect=[(landing,'text/html','https://example.org/'),(pdf_bytes(),'application/pdf','https://example.org/paper.pdf')]) as fetch:
            result=PaperReader().read('https://example.org/')
            self.assertEqual(result['pages'],1)
            self.assertEqual(fetch.call_args.args[0],'https://example.org/paper.pdf')
        with patch('paper_reader.download', return_value=(landing.replace(b'citation_pdf_url',b'ignored'),'text/html','https://example.org/')):
            with self.assertRaises(PaperError): PaperReader().read('https://example.org/')

    def test_html_article_removes_scripts_and_marks_truncation(self):
        html=b'<article><h1>Methods</h1><p>'+b'evidence '*24000+b'</p><h2>Results</h2><script>dangerous</script></article>'
        with patch('paper_reader.download', return_value=(html,'text/html','https://example.org/')):
            result=PaperReader().read('https://example.org/')
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['text']),180000)
        self.assertNotIn('dangerous',result['text'])

    def test_download_limits_and_timeout(self):
        response=MagicMock(status=200)
        response.getheader.side_effect=lambda name,default='': str(MAX_DOWNLOAD+1) if name=='Content-Length' else default
        with patch('paper_reader.public_target',return_value=('example.org','8.8.8.8','https://example.org/')), patch('paper_reader.PublicHTTPSConnection') as conn:
            conn.return_value.getresponse.return_value=response
            with self.assertRaises(PaperError): download('https://example.org/',time.monotonic()+5)
            response.read.assert_not_called()
        with patch('paper_reader.public_target',return_value=('example.org','8.8.8.8','https://example.org/')):
            with self.assertRaises(PaperError): download('https://example.org/',time.monotonic()-1)


if __name__=='__main__': unittest.main()
