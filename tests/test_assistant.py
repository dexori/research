from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app import create_app
from codex_bridge import BridgeError, retrieve_notes


class FakeCodex:
    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.fail = False
        self.available = True

    def status(self):
        return {'available':self.available,'profile':'测试终端','model':'测试模型','read_only':True}

    def answer(self, question, history, sources):
        assert not self.workspace.lock._is_owned(), 'Long CLI invocation must not lock the workspace'
        self.calls.append((question, history, sources))
        if self.fail:
            raise BridgeError('隔离测试：网络错误，问题已保留。')
        return '## 待验证的假设\n\n**讨论草稿**。<script>alert(1)</script>\n\n' + (
            f"参考 [{sources[0]['title']}](/entry/{sources[0]['id']})" if sources else '未检索到相关资料。')


class AssistantTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='assistant-test-')
        self.app = create_app(self.temp.name, testing=True)
        self.bridge = FakeCodex(self.app.extensions['git_workspace'])
        self.app.extensions['codex_bridge'] = self.bridge
        self.client = self.app.test_client()
        self.client.get('/setup')
        self.post('/setup',password='test-research-2026',confirm='test-research-2026')

    def tearDown(self):
        self.temp.cleanup()

    def post(self, route, **data):
        with self.client.session_transaction() as session:
            token = session.get('csrf')
        if not token:
            self.client.get('/')
            with self.client.session_transaction() as session:
                token = session['csrf']
        return self.client.post(route,data={'csrf':token,**data})

    def make(self, kind='paper', **data):
        fields = dict(title='DTW 文献',status='进行中',content='DTW 比较未来动作轨迹。')
        fields.update(data)
        response = self.post('/new/'+kind,**fields)
        self.assertEqual(response.status_code,302)
        return response.location.split('/')[-1]

    def connection(self):
        connection = sqlite3.connect(Path(self.temp.name)/'research.sqlite3')
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def test_auth_csrf_missing_cli_and_input_validation(self):
        anonymous = self.app.test_client()
        for route in ['/assistant','/assistant/thread/unknown']:
            self.assertEqual(anonymous.get(route).location,'/login')
        self.assertEqual(anonymous.post('/assistant/send',data={'message':'DTW'}).location,'/login')
        self.assertEqual(self.client.post('/assistant/send',data={'message':'DTW'}).status_code,400)
        self.assertEqual(self.post('/assistant/send',message='x'*4001).status_code,400)
        self.assertEqual(self.post('/assistant/send',message=' ').status_code,400)
        self.bridge.available = False
        self.assertEqual(self.post('/assistant/send',message='DTW').status_code,503)
        self.assertEqual(self.bridge.calls,[])

    def test_multi_turn_retrieval_persistence_sanitization_and_no_auto_write(self):
        identifier = self.make()
        response = self.post('/assistant/send',message='DTW 方法有什么假设？')
        self.assertEqual(response.status_code,200)
        payload = response.json
        self.assertEqual(len(payload['messages']),2)
        answer = payload['messages'][-1]
        self.assertEqual(answer['sources'][0]['id'],identifier)
        self.assertNotIn('<script>',answer['html'])
        self.assertIn('<strong>讨论草稿</strong>',answer['html'])
        self.assertEqual(self.bridge.calls[0][2][0]['content'],'DTW 比较未来动作轨迹。')
        response = self.post('/assistant/send',message='帮我进一步细化',thread_id=payload['thread_id'])
        self.assertEqual(len(response.json['messages']),4)
        self.assertEqual(len(self.bridge.calls[-1][1]),2)
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT count(*) FROM ai_messages').fetchone()[0],4)
        self.assertIn('DTW'.encode(),self.client.get('/assistant?thread='+payload['thread_id']).data)
        restarted = create_app(self.temp.name,testing=True)
        self.assertEqual(restarted.config['SECRET_KEY'],self.app.config['SECRET_KEY'])

    def test_project_scope_pinned_context_and_cannot_change_thread_scope(self):
        project = self.make('project',title='项目 A')
        other = self.make('project',title='项目 B')
        pinned = self.make(project_id=project,title='归档上下文',status='已归档')
        unrelated = self.make(project_id=other,title='另一个项目')
        self.assertEqual(self.post('/assistant/send',message='DTW',project_id=project,context_id=unrelated).status_code,400)
        self.assertEqual(self.post('/assistant/send',message='DTW',project_id='missing').status_code,400)
        response = self.post('/assistant/send',message='DTW',project_id=project,context_id=pinned)
        self.assertEqual(response.status_code,200)
        source_ids = {source['id'] for source in self.bridge.calls[-1][2]}
        self.assertIn(pinned,source_ids)
        self.assertNotIn(unrelated,source_ids)
        self.post('/assistant/send',message='下一步实验',thread_id=response.json['thread_id'],project_id=other)
        self.assertNotIn(unrelated,{source['id'] for source in self.bridge.calls[-1][2]})

    def test_draft_review_does_not_write_until_standard_save_and_retains_version(self):
        identifier = self.make()
        payload = self.post('/assistant/send',message='DTW').json
        message_id = payload['messages'][-1]['id']
        response = self.post('/assistant/draft',message_id=message_id,kind='idea')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'action="/new/idea"',response.data)
        self.assertIn('尚未保存'.encode(),response.data)
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT content FROM entries WHERE id=?',(identifier,)).fetchone()[0],'DTW 比较未来动作轨迹。')
        response = self.post('/assistant/draft',message_id=message_id,target_id=identifier)
        self.assertIn(f'action="/entry/{identifier}/edit"'.encode(),response.data)
        self.assertIn(b'name="version" value="1"',response.data)
        self.assertIn('DTW 比较未来动作轨迹。'.encode(),response.data)
        other = self.make(title='不可追加的记录',content='different')
        self.assertEqual(self.post('/assistant/draft',message_id=message_id,target_id=other).status_code,400)
        self.assertEqual(self.post('/assistant/draft',message_id=payload['messages'][0]['id']).status_code,404)
        self.assertEqual(self.post('/assistant/draft',message_id=message_id,kind='dataset').status_code,400)
        response = self.post('/new/idea',title='审核后的假设',status='待开始',content='已核对的讨论草稿')
        self.assertEqual(response.status_code,302)
        with self.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0],3)

    def test_errors_retain_question_pending_rejects_and_stale_thread_recovers(self):
        self.bridge.fail = True
        response = self.post('/assistant/send',message='DTW 失败测试')
        self.assertEqual(response.status_code,502)
        payload = response.json
        self.assertEqual(payload['messages'][0]['content'],'DTW 失败测试')
        self.assertEqual(payload['messages'][-1]['status'],'error')
        self.assertEqual(self.post('/assistant/draft',message_id=payload['messages'][-1]['id']).status_code,404)
        with self.connection() as connection:
            connection.execute("UPDATE ai_messages SET status='pending',created_at='2000-01-01T00:00:00' WHERE role='assistant'")
            connection.commit()
        self.assertEqual(self.post('/assistant/send',message='DTW',thread_id=payload['thread_id']).status_code,409)
        refreshed = self.client.get('/assistant/thread/'+payload['thread_id'])
        self.assertEqual(next(message for message in refreshed.json['messages'] if message['role']=='assistant')['status'],'error')
        self.bridge.fail = False
        self.assertEqual(self.post('/assistant/send',message='DTW',thread_id=payload['thread_id']).status_code,200)

    def test_retrieval_bounds_excludes_archived_and_unknown(self):
        archived = self.make(status='已归档')
        with self.connection() as connection:
            self.assertEqual(retrieve_notes(connection,'DTW'),[])
        for i in range(8):
            self.make(title=f'DTW {i}',content='DTW '+('测试正文'*3000))
        with self.connection() as connection:
            sources = retrieve_notes(connection,'DTW 文献')
            self.assertGreater(len(sources),0)
            self.assertLessEqual(len(sources),6)
            self.assertLessEqual(sum(len(item['content']) for item in sources),42000)
            self.assertNotIn(archived,{item['id'] for item in sources})
            self.assertEqual(retrieve_notes(connection,'xyz_unrelated_token'),[])


if __name__ == '__main__':
    unittest.main()
