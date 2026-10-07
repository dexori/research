import io
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from app import create_app
from monitoring import DEFAULT_SERVER, ServerMonitor
from training_metrics import LogReader, MetricHistory, MAX_READ, parse_metrics, source_key
from test_monitoring import FakeClient


class RemoteLog:
    def __init__(self, raw):
        self.raw = raw
        self.read_sizes = []
    def client(self):
        class RemoteFile(io.BytesIO):
            def read(inner, size=-1):
                self.read_sizes.append(size)
                return super().read(size)
        stream = RemoteFile(self.raw)
        stream.stat = lambda: Mock(st_mode=stat.S_IFREG, st_size=len(self.raw))
        sftp = Mock(); sftp.__enter__ = Mock(return_value=sftp); sftp.__exit__ = Mock(return_value=False)
        sftp.open.return_value = stream
        client = Mock(); client.open_sftp.return_value = sftp
        return client


class TrainingMetricTest(unittest.TestCase):
    def test_real_formats_learning_rate_and_missing_values(self):
        text = '\x1b[32mstep=1,000 train_loss=.25 lr=1e-4 epoch=2\x1b[0m\n' + '{"global_step": 1001, "loss": 0.2, "learning_rate": 9e-5}\nstep: 1002 lr: 8e-5\nstep: 1003 loss=NaN lr=inf'
        points = parse_metrics(text)
        self.assertEqual(points[0], {'step': 1000, 'loss': .25, 'lr': .0001, 'epoch': 2.0})
        self.assertEqual(points[1]['lr'], .00009)
        self.assertNotIn('loss', points[2])
        self.assertNotIn('lr', points[-1])
        abbreviated = parse_metrics('step:10K smpl:320K epch:1.25 loss:0.32 lr:1.0e-4')
        self.assertEqual(abbreviated[0]['step'], 10000)
        self.assertTrue(abbreviated[0]['approximate'])
        self.assertEqual(abbreviated[0]['epoch'], 1.25)
        self.assertEqual(parse_metrics('cfg.steps=100000 (100K)'), [])

    def test_log_incremental_partial_numbers_rotation_and_read_limits(self):
        log = RemoteLog(b'step=1 loss=0.3 lr=1e-4\nstep=2 loss=0.')
        reader = LogReader(); first = reader.read(log.client(), '/tmp/train.log')
        self.assertEqual(len(first['metrics']), 1)
        self.assertFalse(first['reset'])
        log.raw += b'2 lr=8e-5\n'
        second = reader.read(log.client(), '/tmp/train.log')
        self.assertEqual(second['metrics'], [{'step': 2, 'loss': .2, 'lr': .00008}])
        self.assertEqual(reader.read(log.client(), '/tmp/train.log')['metrics'], [])
        log.raw = b'step=0 loss=0.5 lr=1e-3\n'
        self.assertTrue(reader.read(log.client(), '/tmp/train.log')['reset'])
        log.raw = b'x' * (MAX_READ + 500) + b'\nstep=99 loss=0.1\n'
        limited = reader.read(log.client(), '/tmp/train.log')
        self.assertTrue(limited['history_limited'])
        self.assertEqual(limited['metrics'][-1]['step'], 99)
        self.assertTrue(all(0 <= n <= MAX_READ for n in log.read_sizes))

    def test_persistent_ssh_cached_queries_and_reconnect(self):
        clients = []
        def factory():
            client = FakeClient(); clients.append(client); return client
        now = [0]; monitor = ServerMonitor(factory, lambda: now[0])
        monitor.connect('a', DEFAULT_SERVER, 'isolated-secret')
        monitor._command = Mock(return_value=('', 0))
        monitor.snapshot('a'); self.assertEqual(monitor._command.call_count, 3)
        now[0] = 1; monitor.snapshot('a')
        self.assertEqual(len(clients), 1)
        self.assertEqual(monitor._command.call_count, 4)  # GPU only, no new handshake.
        now[0] = 6; monitor.snapshot('a')
        self.assertEqual(monitor._command.call_count, 6)  # GPU + process list.
        clients[0].close(); now[0] = 7; monitor.snapshot('a')
        self.assertEqual(len(clients), 2)
        monitor.disconnect('a'); self.assertTrue(clients[-1].closed)


class ExperimentTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='training-history-')
        self.app = create_app(self.tmp.name, testing=True)
        self.client = self.app.test_client(); self.client.get('/setup')
        self.post('/setup', password='test-owner-password', confirm='test-owner-password'); self.client.get('/')
        self.id = self.post('/new/experiment', title='baseline seed0', status='进行中', objective='比较 DTW 辅助损失',
                            model='SmolVLA', dataset='LIBERO', seed='0', params='lambda=0.1',
                            total_steps='1000', batch_size='32', learning_rate='1e-4', training_pid='1234',
                            log_path='/home/zdx/train.log', return_monitor='1').location.split('experiment=')[1]

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, **fields):
        with self.client.session_transaction() as s: token = s['csrf']
        return self.client.post(url, data={'csrf': token, **fields})

    def connection(self):
        db = sqlite3.connect(Path(self.tmp.name) / 'research.sqlite3'); db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON'); return db

    def test_experiment_fields_persist_and_preserve_on_old_editor(self):
        with closing(self.connection()) as db:
            row = db.execute('SELECT * FROM entries WHERE id=?', (self.id,)).fetchone()
            import json
            meta = json.loads(row['metadata'])
            self.assertEqual(meta['total_steps'], '1000')
            self.assertEqual(meta['log_path'], '/home/zdx/train.log')
        self.assertIn('比较 DTW'.encode(), self.client.get('/monitor?experiment=' + self.id).data)
        self.assertIn(b'"total_steps": "1000"', next(Path(self.tmp.name).rglob(self.id + '.md')).read_bytes())
        response = self.post('/entry/' + self.id + '/edit', title='old client', content='kept', status='进行中', version='1')
        self.assertEqual(response.status_code, 302)
        with closing(self.connection()) as db:
            self.assertIn('train.log', db.execute('SELECT metadata FROM entries WHERE id=?', (self.id,)).fetchone()[0])

    def test_invalid_progress_configuration(self):
        for fields in [{'total_steps': '-1'}, {'learning_rate': 'NaN'}, {'batch_size': '1.5'}, {'log_path': '../bad.log'}]:
            response = self.post('/new/experiment', title='invalid', status='进行中', **fields)
            self.assertEqual(response.status_code, 200)
            self.assertIn('error', response.get_data(as_text=True))

    def test_history_deduplicate_segments_sources_and_reopen(self):
        with closing(self.connection()) as db:
            history = MetricHistory(db); source = source_key(DEFAULT_SERVER, '/home/zdx/train.log')
            history.record(self.id, source, [{'step': 10, 'loss': .3}, {'step': 11, 'lr': .0001}])
            result = history.record(self.id, source, [{'step': 10, 'loss': .3}, {'step': 11, 'lr': .0001}])
            self.assertEqual(len(result['points']), 2)
            self.assertEqual(result['segment'], 0)
            result = history.record(self.id, source, [{'step': 1, 'loss': .4, 'lr': .0002}], reset=True)
            self.assertEqual(result['segment'], 1)
            self.assertEqual(result['latest_step'], 1)
            self.assertEqual(len(result['points']), 1)
            self.assertEqual(history.read(self.id, source_key(DEFAULT_SERVER, '/different.log'))['points'], [])
        with closing(self.connection()) as db:
            self.assertEqual(MetricHistory(db).read(self.id, source)['latest_step'], 1)
        response = self.client.get('/monitor/experiments/' + self.id + '/metrics.csv')
        self.assertIn(b'learning_rate', response.data); self.assertIn(b'0.0002', response.data)
        self.assertEqual(self.app.test_client().get('/monitor/experiments/' + self.id + '/metrics.csv').location, '/login')

    def test_archive_real_metrics_to_git_attachment_csrf_and_stale_version(self):
        with closing(self.connection()) as db:
            MetricHistory(db).record(self.id, source_key(DEFAULT_SERVER, '/home/zdx/train.log'),
                                     [{'step': 10, 'loss': .3, 'lr': .0001}])
        url = '/monitor/experiments/' + self.id + '/archive'
        self.assertEqual(self.client.post(url, data={'version': '1'}).status_code, 400)
        response = self.post(url, version='1')
        self.assertEqual(response.status_code, 302)
        with closing(self.connection()) as db:
            file = db.execute('SELECT * FROM attachments WHERE entry_id=?', (self.id,)).fetchone()
            self.assertIn('training-curves', file['name'])
            raw = (Path(self.tmp.name) / 'uploads' / file['storage_name']).read_bytes()
            self.assertIn(b'0.0001', raw)
        markdown = next(Path(self.tmp.name).rglob(self.id + '.md')).read_bytes()
        self.assertIn(file['storage_name'].encode(), markdown)
        self.assertIn(b'"total_steps": "1000"', markdown)
        self.assertEqual(self.post(url, version='1').status_code, 409)

    def test_snapshot_records_only_linked_log_not_another_experiment(self):
        server = Mock(); server.state.return_value = DEFAULT_SERVER
        server.snapshot.return_value = {'server': DEFAULT_SERVER, 'gpus': [], 'processes': [],
            'metrics': [{'step': 20, 'loss': .2, 'lr': .0001}], 'warnings': []}
        self.app.extensions['server_monitor'] = server
        response = self.post('/api/monitor/server', experiment=self.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['history']['latest_step'], 20)
        self.assertEqual(response.json['experiment']['total_steps'], '1000')
        response = self.post('/api/monitor/server', experiment=self.id, log_path='/another.log')
        self.assertNotIn('history', response.json)
        with closing(self.connection()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_points').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
