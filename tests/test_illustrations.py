from contextlib import closing
import io
import zipfile
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from app import create_app
from codex_bridge import BridgeError, CodexBridge
from draft_import import load_draft, stage_draft
from research_images import stage_image, inspect_image, asset_path, insert_illustration

CARD = '# ACT 框架\n\n分类：模型架构\n标签：ACT, CVAE\n\n## 一句话理解\n\n动作块预测。\n\n## 核心原理与数据流\n\n观测产生动作块。\n\n## 最小例子\n\n$$z=f(o)$$\n\n## 来源\n\n[原论文](https://arxiv.org/abs/2304.13705)'


def png():
    output=io.BytesIO()
    Image.new('RGB',(128,72),'#b9e6d7').save(output,format='PNG')
    return output.getvalue()


class IllustrationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='research-images-test-')
        self.directory=Path(self.temp.name)
        self.app=create_app(self.directory,testing=True)
        self.client=self.app.test_client()
        self.client.get('/setup')
        self.post('/setup',password='test-illustration-password',confirm='test-illustration-password')
        self.bridge=Mock()
        self.bridge.status.return_value={'available':True,'model':'fixture','profile':'fixture'}
        self.bridge.generate_image.return_value=png()
        self.bridge.research_topic.return_value={'content':CARD,'search_count':1}
        self.app.extensions['codex_bridge']=self.bridge
        researcher=Mock()
        researcher.research.return_value={'content':CARD,'search_count':1,'source_urls':['https://arxiv.org/abs/2304.13705']}
        self.app.extensions['learning_researcher']=researcher

    def tearDown(self):
        self.temp.cleanup()

    def post(self,path,**fields):
        self.client.get('/')
        with self.client.session_transaction() as s:token=s['csrf']
        return self.client.post(path,data={'csrf':token,**fields})

    def connection(self):
        conn=sqlite3.connect(self.directory/'research.sqlite3');conn.row_factory=sqlite3.Row
        return closing(conn)

    def wait(self,url):
        for _ in range(250):
            result=self.client.get(url).json
            if result['status']!='pending':return result
            time.sleep(.02)
        self.fail('Job timed out')

    def generate(self):
        result=self.wait(self.post('/learning/start',topic='ACT 框架',illustrated='1').json['job_url'])
        self.assertEqual(result['status'],'ready',result)
        return load_draft(self.directory,result['review_url'].split('/')[-1])

    def test_review_save_and_restore_portable_image(self):
        draft=self.generate();image=draft['images'][0]
        self.assertIn('/draft-media/'+image['id'],draft['content'])
        self.assertIn('不是论文原图',draft['content'])
        with self.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM entries').fetchone()[0],0)
        media=self.client.get('/draft-media/'+image['id']);self.assertEqual(media.status_code,200)
        self.assertEqual(media.mimetype,'image/png');self.assertIn('no-store',media.headers['Cache-Control'])
        self.assertIn(b'data-editor-preview',self.client.get('/imports/'+draft['id']).data)
        response=self.post('/new/knowledge',import_id=draft['id'],title='ACT 审核',content=draft['content'],include_image=image['id'])
        self.assertEqual(response.status_code,302)
        with self.connection() as c:
            entry=c.execute('SELECT * FROM entries').fetchone();file=c.execute('SELECT * FROM attachments').fetchone()
        self.assertNotIn('/draft-media/',entry['content'])
        self.assertIn('../attachments/'+file['storage_name']+'/attachment.png',entry['content'])
        self.assertIn(('/media/'+file['storage_name']).encode(),self.client.get(response.location).data)
        self.assertEqual(self.client.get('/media/'+file['storage_name']).data,png())
        # A second computer reconstructs SQLite/uploads using only the canonical Git files.
        second=self.directory/'second'
        restored=create_app(second,testing=True,repo_dir=self.directory/'checkout')
        with sqlite3.connect(second/'research.sqlite3') as c:
            self.assertEqual(c.execute('SELECT content FROM entries').fetchone()[0],entry['content'])
            self.assertEqual(c.execute('SELECT storage_name FROM attachments').fetchone()[0],file['storage_name'])
        self.assertEqual((second/'uploads'/file['storage_name']).read_bytes(),png())
        before=(self.directory/'checkout/workspace/records'/f"{entry['id']}.md").read_bytes()
        self.post('/new/knowledge',import_id=draft['id'],title='duplicate',content='duplicate')
        self.assertEqual((self.directory/'checkout/workspace/records'/f"{entry['id']}.md").read_bytes(),before)

    def test_failure_and_unchecked_image_save_text(self):
        self.bridge.generate_image.side_effect=BridgeError('SECRET_PROVIDER_TOKEN')
        draft=self.generate()
        self.assertEqual(draft['images'],[])
        self.assertIn('文字草稿已保留',draft['content'])
        self.assertNotIn('SECRET',draft['content'])
        self.bridge.generate_image.side_effect=None
        draft=self.generate()
        response=self.post('/new/knowledge',import_id=draft['id'],title='文字',content=draft['content'])
        self.assertEqual(response.status_code,302)
        with self.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attachments').fetchone()[0],0)
            self.assertNotIn('/draft-media/',c.execute('SELECT content FROM entries').fetchone()[0])
            self.assertNotIn('图 1 ·',c.execute('SELECT content FROM entries').fetchone()[0])

    def test_backup_preserves_draft_images_and_older_draft_media(self):
        draft=self.generate();image=draft['images'][0]
        for index in range(105):
            stage_draft(self.directory,'# later '+str(index))
        self.assertEqual(self.client.get('/draft-media/'+image['id']).data,png())
        with closing(self.client.get('/backup/download')) as response:
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(archive.read('draft-assets/'+image['id']+'.png'),png())
                self.assertIn('draft-assets',archive.read('RESTORE.txt').decode())

    def test_regeneration_disk_error_does_not_wait_forever_or_overwrite(self):
        draft=self.generate()
        with patch('app.stage_draft',side_effect=OSError('SECRET_DISK_PATH')):
            result=self.wait(self.post('/imports/'+draft['id']+'/illustrate',content=draft['content']).json['job_url'])
        self.assertEqual(result['status'],'error')
        self.assertNotIn('SECRET',str(result))
        self.assertEqual(load_draft(self.directory,draft['id'])['content'],draft['content'])

    def test_preview_sanitization_auth_and_non_image_media(self):
        external='![external](https://evil.example/test.png)<img src="/media/'+('a'*32)+'" onerror="alert(1)"><script>bad()</script>'
        result=self.post('/markdown/preview',content=external).json['html']
        self.assertNotIn('evil.example',result);self.assertNotIn('onerror',result);self.assertNotIn('<script',result)
        self.assertIn('/media/'+('a'*32),result)
        guest=self.app.test_client()
        for route in ['/media/'+('a'*32),'/draft-media/'+('a'*32)]:self.assertEqual(guest.get(route).location,'/login')
        self.assertEqual(self.client.post('/markdown/preview').status_code,400)
        self.assertEqual(self.post('/markdown/preview',content='a'*200001).status_code,400)
        self.assertEqual(self.client.get('/draft-media/'+('a'*32)).status_code,404)
        with self.assertRaises(ValueError):inspect_image(b'<svg onload="evil()"/>')
        with self.assertRaises(ValueError):inspect_image(png()[:-8])

    def test_foreign_asset_rejected_and_regeneration_preserves_original(self):
        first=self.generate();second=self.generate()
        response=self.post('/new/knowledge',import_id=first['id'],title='bad',content=second['content'],include_image=second['images'][0]['id'])
        self.assertEqual(response.status_code,200)
        self.assertIn('不属于这份草稿'.encode(),response.data)
        changed=first['content']+'\n\n用户修改过的正文'
        result=self.wait(self.post('/imports/'+first['id']+'/illustrate',content=changed,title='编辑后标题').json['job_url'])
        revised=load_draft(self.directory,result['review_url'].split('/')[-1])
        self.assertIn('用户修改过的正文',revised['content'])
        self.assertEqual(revised['title'],'编辑后标题')
        self.assertNotEqual(first['images'][0]['id'],revised['images'][0]['id'])
        self.assertEqual(load_draft(self.directory,first['id'])['content'],first['content'])

    def test_live_assistant_returns_immediately_and_persists_public_updates(self):
        release=threading.Event()
        def answer(question,history,sources,on_progress=None,**selection):
            on_progress({'stage':'generating','detail':'终端已开始回答'})
            on_progress({'stage':'generating','detail':'公开回复已返回','output':'第一段公开回复 $z=f(o)$'})
            release.wait(3)
            return '最终回答'
        self.bridge.answer.side_effect=answer
        try:
            response=self.post('/assistant/send',message='ACT',stream='1')
            self.assertEqual(response.status_code,202)
            url='/assistant/thread/'+response.json['thread_id']
            for _ in range(50):
                payload=self.client.get(url).json
                assistant=payload['messages'][-1]
                if '第一段公开回复' in assistant.get('progress',{}).get('output_html',''):break
                time.sleep(.01)
            self.assertEqual(assistant['status'],'pending')
            self.assertIn('第一段公开回复',assistant['progress']['output_html'])
            self.assertIn('arithmatex',assistant['progress']['output_html'])
            self.assertEqual(self.post('/assistant/send',message='again',stream='1',thread_id=response.json['thread_id']).status_code,409)
        finally:release.set()
        for _ in range(100):
            assistant=self.client.get(url).json['messages'][-1]
            if assistant['status']!='pending':break
            time.sleep(.01)
        self.assertEqual(assistant['status'],'complete');self.assertEqual(assistant['content'],'最终回答')
        self.assertEqual(assistant['progress']['stage'],'complete')


class ImageBridgeTest(unittest.TestCase):
    def test_only_fresh_local_valid_assets_accepted_no_shell_or_api(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'profile';root=profile/'generated_images';root.mkdir(parents=True)
            bridge=CodexBridge(Path(temp)/'data');path=root/'new.png'
            process=Mock();process.returncode=0;process.poll.return_value=0
            def stream(*args):
                path.write_bytes(png()) # deterministic test fixture, not an image-generation substitute
                return json.dumps({'type':'item.completed','item':{'type':'agent_message','text':str(path)}})
            with patch.object(bridge,'profile',return_value=profile),patch('codex_bridge.shutil.which',return_value='/usr/bin/true'),patch('codex_bridge.subprocess.Popen',return_value=process),patch.object(bridge,'stream_output',side_effect=stream):
                cmd=bridge.command(image=True)
                self.assertIn('features.image_generation=true',cmd)
                self.assertIn('features.shell_tool=false',cmd);self.assertIn('features.apps=false',cmd)
                self.assertIn('features.plugins=false',cmd);self.assertIn('read-only',cmd)
                self.assertNotIn('features.code_mode=false',cmd)
                self.assertEqual(bridge.generate_image('test'),png())
                with self.assertRaises(BridgeError):bridge.generate_image('same old file')
                self.assertFalse(bridge.gate.locked())


if __name__=='__main__':unittest.main()
