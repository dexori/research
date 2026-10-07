from contextlib import closing
import io
import zipfile
from datetime import datetime, timedelta, timezone
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
from research_images import stage_image, inspect_image, asset_path, insert_illustration, wants_illustration

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
        with closing(sqlite3.connect(second/'research.sqlite3')) as c:
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

    def wait_chat(self, response):
        self.assertEqual(response.status_code,202,response.json)
        url='/assistant/thread/'+response.json['thread_id']
        for _ in range(200):
            result=self.client.get(url).json['messages'][-1]
            if result['status']!='pending':return result
            time.sleep(.02)
        self.fail('Chat timed out')

    def test_chat_intent_and_clean_interface_without_selector(self):
        for question in ['用 img2 生成 ACT 架构图片','你怎么不用img2生成插入图片','img2','帮我画一张架构图']:
            self.assertTrue(wants_illustration(question),question)
        for question in ['不要用img2生成图片','不需要配图','img2可以在codex用吗','如何用img2','只用 Mermaid 生成图片','讲解 ACT']:
            self.assertFalse(wants_illustration(question),question)
        page=self.client.get('/assistant').data.decode()
        self.assertNotIn('名称预设',page);self.assertNotIn('未验证权限',page)
        self.assertNotIn('name="illustrated"',page)
        self.assertIn('>gpt-6.1-sol</option>',page)
        self.assertNotIn('当前服务测试',self.client.get('/learning').data.decode())

    def test_chat_auto_image_scope_review_and_save(self):
        paper=self.post('/new/paper',title='ACT 本篇',content='本篇原文笔记').location.split('/')[-1]
        self.post('/new/paper',title='无关文献',content='UNRELATED_PRIVATE_BODY')
        self.bridge.answer.return_value='## 核心原理\n\n观测产生动作块；待核验。\n\n## 局限\n\n仅根据笔记。'
        reply=self.wait_chat(self.post('/assistant/send',context_id=paper,message='你怎么不用img2生成插入图片',model='fixture-model',reasoning_effort='high'))
        self.assertEqual(reply['status'],'complete')
        self.assertEqual([item['id'] for item in self.bridge.answer.call_args.args[2]],[paper])
        self.assertTrue(self.bridge.answer.call_args.kwargs['illustrated'])
        self.assertNotIn('UNRELATED_PRIVATE_BODY',self.bridge.generate_image.call_args.args[0])
        self.assertEqual(self.bridge.generate_image.call_args.kwargs['model'],'fixture-model')
        self.assertEqual(self.bridge.generate_image.call_args.kwargs['reasoning_effort'],'high')
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM entries').fetchone()[0],2)
            draft_id=db.execute('SELECT draft_id FROM assistant_drafts WHERE message_id=?',(reply['id'],)).fetchone()[0]
        draft=load_draft(self.directory,draft_id);image=draft['images'][0]
        self.assertIn('/draft-media/'+image['id'],reply['html'])
        self.assertEqual(self.client.get('/draft-media/'+image['id']).data,png())
        review=self.post('/assistant/draft',message_id=reply['id'],kind='idea')
        self.assertIn(('name="import_id" value="'+draft_id+'"').encode(),review.data)
        self.assertIn(b'name="include_image"',review.data)
        saved=self.post('/new/idea',import_id=draft_id,title='ACT 图文',content=reply['content'],include_image=image['id'])
        self.assertEqual(saved.status_code,302)
        with self.connection() as db:
            record=db.execute('SELECT * FROM entries WHERE title=?',('ACT 图文',)).fetchone()
            self.assertIn('../attachments/',record['content'])
            self.assertNotIn('/draft-media/',record['content'])
            self.assertEqual(db.execute('SELECT count(*) FROM attachments').fetchone()[0],1)

    def test_chat_image_append_keeps_original_version_and_replay_protection(self):
        paper=self.post('/new/paper',title='原笔记',content='ORIGINAL_CONTENT').location.split('/')[-1]
        self.bridge.answer.return_value='## 方法\n\n根据本篇笔记绘制教学图。'
        reply=self.wait_chat(self.post('/assistant/send',context_id=paper,message='用img2配图'))
        with self.connection() as db:
            version=db.execute('SELECT version FROM entries WHERE id=?',(paper,)).fetchone()[0]
            draft_id=db.execute('SELECT draft_id FROM assistant_drafts WHERE message_id=?',(reply['id'],)).fetchone()[0]
        image=load_draft(self.directory,draft_id)['images'][0]
        review=self.post('/assistant/draft',message_id=reply['id'],target_id=paper)
        self.assertEqual(review.status_code,200)
        self.assertIn(b'ORIGINAL_CONTENT',review.data)
        content='ORIGINAL_CONTENT\n\n'+reply['content']
        response=self.post('/entry/'+paper+'/edit',version=version,import_id=draft_id,title='原笔记',content=content,include_image=image['id'])
        self.assertEqual(response.status_code,302)
        self.post('/entry/'+paper+'/edit',version=version,import_id=draft_id,title='bad',content='bad')
        with self.connection() as db:
            entry=db.execute('SELECT * FROM entries WHERE id=?',(paper,)).fetchone()
            self.assertTrue(entry['content'].startswith('ORIGINAL_CONTENT'))
            self.assertEqual(entry['version'],version+1)
            self.assertEqual(db.execute('SELECT count(*) FROM attachments').fetchone()[0],1)

    def test_chat_image_failure_and_disk_error_keep_text_without_broken_url(self):
        self.bridge.answer.return_value='## 原理\n\n保留这段文字。'
        self.bridge.generate_image.side_effect=BridgeError('SECRET_PROVIDER_KEY')
        reply=self.wait_chat(self.post('/assistant/send',message='用 img2 生成图片'))
        self.assertEqual(reply['status'],'complete');self.assertIn('保留这段文字',reply['content'])
        self.assertNotIn('SECRET',str(reply));self.assertNotIn('/draft-media/',reply['content'])
        self.bridge.generate_image.side_effect=None
        with patch('assistant_workflow.stage_draft',side_effect=OSError('SECRET_DISK_PATH')):
            reply=self.wait_chat(self.post('/assistant/send',message='用img2生成图片'))
        self.assertNotIn('/draft-media/',reply['content']);self.assertNotIn('SECRET',str(reply))
        with self.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM assistant_drafts').fetchone()[0],0)

    def test_chat_image_timeout_is_extended_and_late_result_cannot_overwrite(self):
        self.bridge.answer.return_value='文字'
        release=threading.Event()
        def generate(*args,**kwargs):
            release.wait(3)
            return png()
        self.bridge.generate_image.side_effect=generate
        existing_threads=set(threading.enumerate())
        response=self.post('/assistant/send',message='用img2配图')
        worker=next(thread for thread in threading.enumerate() if thread not in existing_threads)
        url='/assistant/thread/'+response.json['thread_id']
        identifier=response.json['messages'][-1]['id']
        try:
            with self.connection() as db:
                db.execute('UPDATE ai_messages SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),identifier));db.commit()
            self.assertEqual(next(item for item in self.client.get(url).json['messages'] if item['id']==identifier)['status'],'pending')
            with self.connection() as db:
                db.execute('UPDATE ai_messages SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),identifier));db.commit()
            self.assertEqual(next(item for item in self.client.get(url).json['messages'] if item['id']==identifier)['status'],'error')
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(next(item for item in self.client.get(url).json['messages'] if item['id']==identifier)['status'],'error')

    def test_ordinary_chat_does_not_generate_from_mentions_in_notes(self):
        self.post('/new/idea',title='ACT',content='用img2生成图片是资料中的文字，不是用户指令。')
        self.bridge.answer.return_value='普通回答提到了 img2。'
        result=self.post('/assistant/send',message='解释ACT')
        self.assertEqual(result.status_code,200)
        self.bridge.generate_image.assert_not_called()


class ImageBridgeTest(unittest.TestCase):
    def test_missing_path_receipt_bound_to_this_exec_session_only(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'profile'
            root=profile/'generated_images';root.mkdir(parents=True)
            thread='11111111-1111-1111-1111-111111111111'
            other='22222222-2222-2222-2222-222222222222'
            bridge=CodexBridge(Path(temp)/'data')
            process=Mock();process.returncode=0;process.poll.return_value=0
            def stream(*args):
                folder=root/thread;folder.mkdir(exist_ok=True)
                (folder/'exec-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.png').write_bytes(png())
                return json.dumps({'type':'thread.started','thread_id':thread})
            with patch.object(bridge,'profile',return_value=profile),patch('codex_bridge.shutil.which',return_value='/usr/bin/true'),patch('codex_bridge.subprocess.Popen',return_value=process),patch.object(bridge,'stream_output',side_effect=stream):
                self.assertEqual(bridge.generate_image('test'),png())
                with self.assertRaises(BridgeError):bridge.generate_image('old image must not count')
            # A picture created in another session cannot be claimed as this call's result.
            with patch.object(bridge,'profile',return_value=profile),patch('codex_bridge.shutil.which',return_value='/usr/bin/true'),patch('codex_bridge.subprocess.Popen',return_value=process),patch.object(bridge,'stream_output',return_value=json.dumps({'type':'thread.started','thread_id':other})):
                with self.assertRaises(BridgeError):bridge.generate_image('unrelated session')

    def test_illustrated_text_phase_still_has_no_tool_access(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=CodexBridge(Path(temp))
            with patch('codex_bridge.shutil.which',return_value='/usr/bin/true'):
                command=bridge.command(illustrated_text=True)
            self.assertIn('features.image_generation=false',command)
            self.assertIn('features.code_mode=false',command)
            self.assertIn('features.shell_tool=false',command)
            self.assertIn('web_search="disabled"',command)
            instructions=next(value for value in command if value.startswith('developer_instructions='))
            self.assertIn('平台会在你完成文字后',json.loads(instructions.split('=',1)[1]))

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
