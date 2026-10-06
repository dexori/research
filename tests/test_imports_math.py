from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
import zipfile

from app import create_app
from draft_import import MAX_BYTES, decode_text, stage_draft, load_draft, list_drafts
from research_cli import main as cli_main


class ImportMathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='research-import-test-')
        self.directory = Path(self.temp.name)
        self.app = create_app(self.directory,testing=True)
        self.client = self.app.test_client()
        self.client.get('/setup')
        self.post('/setup',password='test-import-2026',confirm='test-import-2026')

    def tearDown(self):
        self.temp.cleanup()

    def post(self,route,**fields):
        self.client.get('/')
        with self.client.session_transaction() as session:
            token = session['csrf']
        return self.client.post(route,data={'csrf':token,**fields})

    def connection(self):
        connection = sqlite3.connect(self.directory/'research.sqlite3')
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def test_cli_stages_without_platform_database_or_record_changes(self):
        data_dir = self.directory/'offline-cache'
        source = self.directory/'讨论 草稿.md'
        source.write_text('# 动作表示假设\n\n公式 $z = f(o)$，尚未验证。',encoding='utf-8')
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main(['--data-dir',str(data_dir),'--port','8766','import',str(source),'--kind','paper'])
        self.assertEqual(result,0)
        match = re.search(r'http://127.0.0.1:8766/imports/([a-f0-9]{32})',output.getvalue())
        self.assertIsNotNone(match)
        draft = load_draft(data_dir,match[1])
        self.assertEqual(draft['title'],'动作表示假设')
        self.assertEqual(draft['source_name'],'讨论 草稿.md')
        self.assertFalse((data_dir/'research.sqlite3').exists())
        self.assertFalse((data_dir/'workspace').exists())
        self.assertIn('尚未写入'.encode(),output.getvalue().encode())
        bad = self.directory/'credentials.json'
        bad.write_text('do not import',encoding='utf-8')
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(['--data-dir',str(data_dir),'import',str(bad)]),1)

    def test_upload_preview_review_save_idempotent_and_original_preserved(self):
        old = self.post('/new/paper',title='已有资料',content='不可覆盖的原文',status='进行中').location.split('/')[-1]
        before = (self.directory/'checkout/workspace/records'/f'{old}.md').read_bytes()
        response = self.post('/imports',file=(io.BytesIO(b'\xef\xbb\xbf# Draft\r\n\r\n$z=f(o)$\r\n'),'draft.md'),kind='idea')
        self.assertEqual(response.status_code,302)
        identifier = response.location.split('/')[-1]
        page = self.client.get(response.location)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'class="arithmatex"',page.data)
        self.assertIn(b'name="import_id"',page.data)
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],1)
        self.assertEqual(self.client.get(response.location+'?kind=paper').status_code,200)
        # Invalid save must preserve draft provenance so a corrected retry remains idempotent.
        bad = self.post('/new/paper',title='',content='草稿',status='待开始',import_id=identifier)
        self.assertIn(f'value="{identifier}"'.encode(),bad.data)
        saved = self.post('/new/paper',title='审核后的文献笔记',content='$z=f(o)$',status='待开始',import_id=identifier)
        self.assertEqual(saved.status_code,302)
        repeated = self.post('/new/paper',title='不能覆盖',content='不同内容',status='待开始',import_id=identifier)
        self.assertEqual(repeated.location,saved.location)
        self.assertEqual(self.client.get('/imports/'+identifier).location,saved.location)
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],2)
            self.assertEqual(connection.execute('SELECT count(*) FROM draft_imports').fetchone()[0],1)
        self.assertEqual((self.directory/'checkout/workspace/records'/f'{old}.md').read_bytes(),before)

    def test_import_auth_csrf_validation_and_untrusted_paths(self):
        other = self.app.test_client()
        self.assertEqual(other.get('/imports').location,'/login')
        self.assertEqual(other.post('/imports').location,'/login')
        self.assertEqual(self.client.post('/imports',data={'content':'draft'}).status_code,400)
        for filename,raw in [('bad.pdf',b'hello'),('empty.md',b''),('encoding.txt',b'\xff\xfe'),('nul.md',b'a\x00b')]:
            response = self.post('/imports',file=(io.BytesIO(raw),filename),kind='idea')
            self.assertEqual(response.status_code,200)
            self.assertIn(b'notice error',response.data)
        self.assertEqual(list_drafts(self.directory),[])
        response = self.post('/imports',content='# 本机草稿',kind='idea')
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.post('/new/idea',title='bad',status='待开始',content='bad',import_id='../session.key').status_code,400)
        self.assertEqual(self.client.get('/imports/not-a-valid-id').status_code,400)
        self.assertEqual(self.client.get('/imports/'+'a'*32).status_code,404)
        self.assertEqual(self.client.get(response.location+'?kind=unknown').status_code,400)

    def test_size_limits_schema_and_symlink(self):
        with self.assertRaises(ValueError):
            decode_text(b'x'*(MAX_BYTES+1))
        with self.assertRaises(ValueError):
            stage_draft(self.directory,'draft',project_id='../unsafe')
        with self.assertRaises(ValueError):
            stage_draft(self.directory,'draft',title='x'*241)
        draft = stage_draft(self.directory,'# 正常草稿',source_name='/private/path/draft.md')
        self.assertEqual(draft['source_name'],'draft.md')
        path = self.directory/'import-drafts'/('b'*32+'.json')
        path.symlink_to(self.directory/'session.key')
        with self.assertRaises(ValueError):
            load_draft(self.directory,'b'*32)
        self.assertEqual(len(list_drafts(self.directory)),1)

    def test_math_preserves_tex_code_and_sanitizes_html(self):
        text = r'''行内 $z_t = f_\theta(o_t)$ 和 \(a_1^2\)。

$$
L = \frac{a}{b}
$$

\[x < y\]

代码 `$literal$`，美元 \$5。

```text
\(not_math\)
```

<span class="evil" onclick="alert(1)">bad</span><script>alert(1)</script>
'''
        render = self.app.jinja_env.filters['markdown']
        html = str(render(text))
        self.assertEqual(html.count('class="arithmatex"'),4)
        self.assertIn(r'\theta',html)
        self.assertIn(r'\frac{a}{b}',html)
        self.assertIn('<code>$literal$</code>',html)
        self.assertIn(r'\(not_math\)',html)
        for unsafe in ['onclick=','class="evil"','<script>']:
            self.assertNotIn(unsafe,html)

    def test_backup_includes_unsaved_drafts_and_saved_notes_portable(self):
        draft = stage_draft(self.directory,'# ZIP 中的未保存草稿\n\n$$a=b$$')
        response = self.client.get('/backup/download')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            imported = json.loads(archive.read('import-drafts/'+draft['id']+'.json'))
            self.assertEqual(imported['content'],draft['content'])
            self.assertIn('import-drafts',archive.read('RESTORE.txt').decode())
            self.assertNotIn('session.key',archive.namelist())
        response.close()
        saved = self.post('/new/idea',title=draft['title'],content=draft['content'],status='待开始',import_id=draft['id'])
        entry_id = saved.location.split('/')[-1]
        self.assertTrue((self.directory/'checkout/workspace/records'/f'{entry_id}.md').exists())


if __name__=='__main__':
    unittest.main()
