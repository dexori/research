from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app import create_app
from codex_bridge import BridgeError, CodexBridge
from draft_import import load_draft
from learning import prepare_card, PublicLearningSearch
from paper_reader import PaperError

CARD = '# ACT 框架\n\n分类：模型架构\n标签：ACT, CVAE, action chunking\n\n## 原理\n\n$z=f(o)$\n\n[原论文](https://arxiv.org/abs/2304.13705)\n\n[项目](https://tonyzhaozh.github.io/aloha/)'


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='research-learning-test-')
        self.directory = Path(self.temp.name)
        self.app = create_app(self.directory, testing=True)
        self.client = self.app.test_client()
        self.client.get('/setup')
        self.post('/setup', password='test-learning-2026', confirm='test-learning-2026')
        self.bridge = Mock()
        self.bridge.status.return_value = {'available': True, 'profile':'测试终端','model':'测试模型'}
        self.bridge.research_topic.return_value = {'content': CARD, 'search_count':2}
        self.app.extensions['codex_bridge'] = self.bridge
        researcher = Mock()
        researcher.research.side_effect=lambda topic, bridge, on_progress=None: bridge.research_topic(topic)
        self.app.extensions['learning_researcher'] = researcher

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, **fields):
        self.client.get('/')
        with self.client.session_transaction() as session:
            token = session['csrf']
        return self.client.post(path, data={'csrf': token, **fields})

    def connection(self):
        connection = sqlite3.connect(self.directory/'research.sqlite3')
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def wait_job(self, url):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            result = self.client.get(url).json
            if result['status']!='pending':
                return result
            time.sleep(.01)
        self.fail('Learning job did not finish')

    def test_topic_only_review_save_metadata_and_no_auto_write(self):
        response = self.post('/learning/start', topic='ACT 框架')
        self.assertEqual(response.status_code, 202)
        result = self.wait_job(response.json['job_url'])
        self.assertEqual(result['status'], 'ready', result)
        self.bridge.research_topic.assert_called_once_with('ACT 框架')
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0], 0)
        draft_id = result['review_url'].split('/')[-1]
        draft = load_draft(self.directory, draft_id)
        page = self.client.get(result['review_url'])
        self.assertIn('审核知识卡'.encode(), page.data)
        self.assertIn(b'name="knowledge_category"', page.data)
        self.assertIn(b'katex', page.data)
        saved = self.post('/new/knowledge', import_id=draft_id, title='审核后的 ACT', content=draft['content'],
                          knowledge_category='模型架构', tags=draft['tags'], status='进行中', search_count='FORGED')
        self.assertEqual(saved.status_code, 302)
        identifier = saved.location.split('/')[-1]
        with self.connection() as connection:
            entry = dict(connection.execute('SELECT * FROM entries WHERE id=?', (identifier,)).fetchone())
        meta = json.loads(entry['metadata'])
        self.assertEqual(meta['knowledge_category'], '模型架构')
        self.assertEqual(meta['learning_topic'], 'ACT 框架')
        self.assertEqual(meta['search_count'], '2')
        record = self.directory/'checkout/workspace/records'/f'{identifier}.md'
        before = record.read_bytes()
        self.assertIn(b'knowledge_category', before)
        self.assertEqual(self.client.get(result['review_url']).location, saved.location)
        self.assertEqual(self.post('/new/knowledge', import_id=draft_id, title='不能重存').location, saved.location)
        self.assertEqual(record.read_bytes(), before)
        match = self.client.get('/library/knowledge?q=ACT&category=模型架构')
        self.assertIn('审核后的 ACT'.encode(), match.data)
        other = self.client.get('/library/knowledge?category=数学基础')
        self.assertNotIn('审核后的 ACT'.encode(), other.data)
        self.assertEqual(self.client.get('/library/knowledge?category=invalid').status_code, 400)
        edited = self.post(f'/entry/{identifier}/edit', title='ACT 修订', content=draft['content'], version=entry['version'],
                           knowledge_category='训练方法')
        self.assertEqual(edited.status_code, 302)
        with self.connection() as connection:
            meta = json.loads(connection.execute('SELECT metadata FROM entries WHERE id=?',(identifier,)).fetchone()[0])
        self.assertEqual(meta['search_count'], '2')
        self.assertEqual(meta['knowledge_category'], '训练方法')

    def test_auth_csrf_input_and_failed_research_do_not_save(self):
        other = self.app.test_client()
        for path in ['/learning', '/learning/job/anything']:
            self.assertEqual(other.get(path).location, '/login')
        self.assertEqual(other.post('/learning/start').location, '/login')
        self.assertEqual(self.client.post('/learning/start', data={'topic':'ACT'}).status_code, 400)
        for topic in ['', 'a'*241]:
            self.assertEqual(self.post('/learning/start', topic=topic).status_code, 400)
        self.assertEqual(self.post('/learning/start', topic='ACT', project_id='bad').status_code, 400)
        self.bridge.research_topic.side_effect = BridgeError('当前服务没有搜索记录')
        result = self.wait_job(self.post('/learning/start',topic='ACT').json['job_url'])
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['review_url'])
        self.assertIn('没有搜索记录', result['error'])
        self.assertEqual(list((self.directory/'import-drafts').glob('*.json')), [])
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0], 0)
        self.assertEqual(self.client.get('/learning/job/missing').status_code, 404)

    def test_pending_duplicate_reload_and_stale_task(self):
        release = threading.Event()
        def slow(topic):
            release.wait(5)
            return {'content':CARD,'search_count':1}
        self.bridge.research_topic.side_effect = slow
        try:
            response = self.post('/learning/start',topic='ACT')
            self.assertEqual(self.client.get(response.json['job_url']).json['status'], 'pending')
            self.assertEqual(self.post('/learning/start',topic='π0').status_code, 409)
            self.assertIn(b'data-learning-job',self.client.get('/learning').data)
        finally:
            release.set()
            self.wait_job(response.json['job_url'])
        with self.connection() as connection:
            connection.execute("INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,?,?)", ('stale','old',None,'pending',None,'','2000-01-01','2000-01-01'))
            connection.commit()
        stale = self.client.get('/learning/job/stale').json
        self.assertEqual(stale['status'], 'error')
        self.assertIn('中断或超时',stale['error'])

    def test_source_validation_and_receipt(self):
        card = prepare_card('ACT', {'content':CARD,'search_count':2})
        self.assertEqual(card['category'], '模型架构')
        self.assertIn('AI 草稿',card['tags'])
        self.assertIn('待人工核验',card['content'])
        for result in [{'content':CARD,'search_count':0}, {'content':'模型声称查过','search_count':1},
                       {'content':'[local](https://127.0.0.1/x)','search_count':1},
                       {'content':'[secret](https://token@example.org/x)','search_count':1}]:
            with self.assertRaises(ValueError):
                prepare_card('ACT',result)

    def test_public_progress_persists_on_reload_and_preview_is_sanitized(self):
        release = threading.Event()
        def research(topic, bridge, on_progress=None):
            on_progress({'stage':'searching','detail':'已找到论文','sources':[
                {'title':'ACT','url':'https://arxiv.org/abs/2304.13705','scope':'待读取'},
                {'title':'unsafe','url':'javascript:alert(1)'},
                {'title':'secret','url':'https://token@example.org'}]})
            on_progress({'stage':'generating','detail':'生成中','output':'<script>alert(1)</script>\n\n$z=f(o)$'})
            release.wait(5)
            return {'content':CARD,'search_count':2}
        self.app.extensions['learning_researcher'].research.side_effect=research
        response=self.post('/learning/start',topic='ACT')
        try:
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                result=self.client.get(response.json['job_url']).json
                if result['progress']['stage']=='generating':
                    break
                time.sleep(.01)
            self.assertEqual(result['status'],'pending')
            self.assertEqual(result['progress']['stage'],'generating')
            self.assertEqual(len(result['progress']['sources']),1)
            self.assertNotIn('<script',result['progress']['output_html'])
            self.assertIn('arithmatex',result['progress']['output_html'])
            self.assertIsInstance(result['elapsed_seconds'],int)
            page=self.client.get('/learning?job='+response.json['job_url'].split('/')[-1])
            self.assertIn(response.json['job_url'].encode(),page.data)
            with self.connection() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],0)
        finally:
            release.set()
            final=self.wait_job(response.json['job_url'])
        self.assertEqual(final['progress']['stage'],'ready')
        self.assertEqual([e['stage'] for e in final['progress']['events']],
                         ['queued','searching','generating','validating','ready'])

    def test_research_command_is_invocation_only_and_requires_events(self):
        bridge = CodexBridge(self.directory)
        with patch('codex_bridge.shutil.which',return_value='/usr/bin/true'), patch.object(bridge,'config',return_value={'mcp_servers':{'test':{}}}):
            command = bridge.command(research=True)
            ordinary = bridge.command()
        self.assertIn('web_search="live"',command)
        self.assertIn('web_search="disabled"',ordinary)
        for restriction in ['features.shell_tool=false','features.plugins=false','mcp_servers.test.enabled=false','read-only']:
            self.assertIn(restriction,command)
        process = Mock(returncode=0)
        process.poll.return_value=0
        answer = {'type':'item.completed','item':{'type':'agent_message','text':CARD}}
        with patch.object(bridge,'command',return_value=['/test']), patch('codex_bridge.subprocess.Popen',return_value=process):
            process.communicate.return_value=(json.dumps(answer),None)
            with self.assertRaisesRegex(BridgeError,'没有观察到'):
                bridge.research_topic('ACT')
            process.communicate.return_value=('\n'.join(json.dumps(x) for x in [
                {'type':'item.completed','item':{'type':'web_search','query':'ACT original paper','status':'completed'}}, answer]),None)
            result=bridge.research_topic('ACT')
            self.assertEqual(result['search_count'],1)
            self.assertEqual(result['content'],CARD)
            process.communicate.return_value=('\n'.join(json.dumps(x) for x in [
                {'type':'item.completed','item':{'type':'web_search','status':'failed'}}, answer]),None)
            with self.assertRaises(BridgeError):
                bridge.research_topic('ACT')

    def test_public_search_validates_feed_and_never_uses_returned_arbitrary_hosts(self):
        search = PublicLearningSearch()
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2304.13705v1</id><title>ACT Paper</title><summary>real abstract</summary></entry><entry><id>https://127.0.0.1/secret</id><title>bad</title></entry></feed>'''
        with patch('learning.download',return_value=(feed,'application/atom+xml','https://export.arxiv.org/api/query')) as fetch:
            sources=search.search('ACT')
            self.assertEqual(len(sources),1)
            self.assertEqual(sources[0]['url'],'https://arxiv.org/abs/2304.13705v1')
            self.assertTrue(fetch.call_args.args[0].startswith('https://export.arxiv.org/api/query?'))
        for payload in [b'<!DOCTYPE feed><feed/>', b'invalid XML', b'<feed/>']:
            search.last_request=0
            with patch('learning.download',return_value=(payload,'','')), self.assertRaises(BridgeError):
                search.search('ACT')

    def test_public_research_passes_only_actual_text_and_rejects_extra_citations(self):
        search=PublicLearningSearch()
        sources=[{'url':'https://arxiv.org/abs/2304.13705','title':'ACT','summary':'abstract','published':'2023'}]
        bridge=Mock()
        bridge.answer.return_value='# ACT\n\n分类：模型架构\n\n[论文](https://arxiv.org/abs/2304.13705)'
        with patch.object(search,'search',return_value=sources), patch('learning.PaperReader') as reader:
            reader.return_value.read.return_value={'text':'actual original paper '*3000,'method':'PDF 提取','pages':10,'pages_read':10,'truncated':False}
            result=search.research('ACT框架',bridge)
            sent=bridge.answer.call_args.args[2]
            self.assertEqual(len(sent),1)
            self.assertTrue(sent[0]['reading']['context_truncated'])
            self.assertEqual(len(sent[0]['content']),40000)
            self.assertTrue(bridge.answer.call_args.kwargs['sourced_learning'])
            self.assertEqual(result['search_count'],1)
            self.assertIn('平台实际检索来源',result['content'])
            self.assertIn('正文有截断',result['content'])
            self.assertEqual(prepare_card('ACT',result)['metadata']['search_method'],'平台 arXiv 文献检索 + 正文提取')
            result['content']+='\n[虚构来源](https://example.org/unknown)'
            with self.assertRaises(ValueError):prepare_card('ACT',result)
            bridge.reset_mock()
            reader.return_value.read.side_effect=PaperError('failed')
            with self.assertRaisesRegex(BridgeError,'正文读取失败'):
                search.research('ACT',bridge)
            bridge.answer.assert_not_called()

    def test_pipeline_reports_actual_sources_and_reading_stages(self):
        search=PublicLearningSearch()
        bridge=Mock()
        bridge.answer.return_value='# ACT\n\n[论文](https://arxiv.org/abs/2304.13705)'
        observed=[]
        with patch.object(search,'search',return_value=[{'url':'https://arxiv.org/abs/2304.13705','title':'ACT','summary':'abstract'}]), patch('learning.PaperReader') as reader:
            reader.return_value.read.return_value={'text':'full paper '*5000,'method':'PDF 提取','pages':10,'pages_read':10,'truncated':False}
            result=search.research('ACT',bridge,on_progress=lambda event:observed.append(json.loads(json.dumps(event))))
        self.assertEqual([event['stage'] for event in observed],
                         ['planning','searching','searching','reading','reading','generating'])
        self.assertEqual(observed[2]['sources'][0]['scope'],'待读取')
        self.assertIn('有截断',observed[-1]['sources'][0]['scope'])
        self.assertIn('40,000',observed[-1]['sources'][0]['scope'])
        self.assertEqual(observed[-1]['sources'][0]['url'],result['source_urls'][0])
        self.assertIn('on_progress',bridge.answer.call_args.kwargs)

    def test_unknown_topic_query_and_sourced_command_are_restricted(self):
        search=PublicLearningSearch()
        bridge=Mock()
        bridge.answer.return_value='```json\n{"keywords":["diffusion policy","robot"]}\n```'
        self.assertEqual(search.query('机器人扩散策略',bridge),'all:"diffusion policy" AND all:"robot"')
        bridge.answer.return_value='{"keywords":["unsafe\" OR all:everything"]}'
        with self.assertRaises(BridgeError):search.query('中文主题',bridge)
        native=CodexBridge(self.directory)
        with patch('codex_bridge.shutil.which',return_value='/usr/bin/true'), patch.object(native,'config',return_value={}):
            command=native.command(sourced_learning=True)
        self.assertIn('web_search="disabled"',command)
        self.assertIn('features.shell_tool=false',command)
        self.assertTrue(any('平台已从 arXiv' in value for value in command if value.startswith('developer_instructions=')) or any('arXiv' in value for value in command if value.startswith('developer_instructions=')))
