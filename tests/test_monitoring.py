import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import paramiko

from app import create_app
from monitoring import (DEFAULT_SERVER, MAX_OUTPUT, MonitorError, ServerMonitor,
                        parse_gpus, parse_metrics, validate_log_path, validate_server)


class FakeClient:
    def __init__(self):
        self.closed = False
        self.params = None
        self.policy = None
        self.transport = Mock()
        self.transport.is_active.side_effect = lambda: not self.closed and self.params is not None
    def load_system_host_keys(self):
        pass
    def set_missing_host_key_policy(self, policy):
        self.policy = policy
    def connect(self, host, **params):
        self.params = params
    def close(self):
        self.closed = True
    def get_transport(self):
        return self.transport


class MonitorUnitTest(unittest.TestCase):
    def test_validation_and_parsers(self):
        self.assertEqual(validate_server(DEFAULT_SERVER), DEFAULT_SERVER)
        for fields in [{**DEFAULT_SERVER, 'port': 0}, {**DEFAULT_SERVER, 'host': '-oProxyCommand=bad'},
                       {**DEFAULT_SERVER, 'user': 'zdx; touch /tmp/bad'}]:
            with self.assertRaises(MonitorError): validate_server(fields)
        self.assertEqual(validate_log_path('/home/zdx/run name/train.log'), '/home/zdx/run name/train.log')
        for path in ['relative.log', '/proc/self/fd/1.log', '/home/zdx/../key.log', '/tmp/no\n.log', '/home/zdx/.ssh/id_rsa']:
            with self.assertRaises(MonitorError): validate_log_path(path)
        parsed = parse_gpus('0, NVIDIA RTX 4090, 92, 14000, 24000, 60, 290\n1, GPU, N/A, 0, 100, N/A, N/A')
        self.assertEqual(parsed[0]['utilization'], 92)
        self.assertIsNone(parsed[1]['temperature'])
        self.assertEqual(parse_metrics('step=10 loss=0.32\nstep: 11 train/loss: 2e-2\nloss=NaN\n')[1], {'step': 11, 'loss': .02})
        self.assertEqual(parse_metrics('loss=0.1 (step unknown)'), [])
        self.assertEqual(parse_metrics('{"global_step": 42, "loss": 0.15}'), [{'step': 42, 'loss': .15}])

    def test_known_hosts_credential_lifetime_and_disconnect(self):
        now = [0]
        clients = []
        def factory():
            c = FakeClient(); clients.append(c); return c
        service = ServerMonitor(factory, lambda: now[0])
        service.connect('one', DEFAULT_SERVER, 'isolated-secret')
        self.assertIsInstance(clients[0].policy, paramiko.RejectPolicy)
        self.assertFalse(clients[0].params['allow_agent'])
        self.assertEqual(clients[0].params['password'], 'isolated-secret')
        self.assertFalse(clients[0].closed)
        self.assertNotIn('password', service.state('one'))
        self.assertIsNone(service.state('other'))
        now[0] = 28801
        self.assertIsNone(service.state('one'))
        self.assertTrue(clients[0].closed)
        self.assertEqual(service._sessions, {})
        service.connect('one', DEFAULT_SERVER)
        self.assertTrue(clients[-1].params['allow_agent'])
        service.disconnect('one')
        self.assertIsNone(service.state('one'))

    def test_errors_hide_provider_details_and_close_connection(self):
        client = FakeClient()
        client.connect = Mock(side_effect=paramiko.AuthenticationException('isolated-secret'))
        service = ServerMonitor(lambda: client)
        with self.assertRaisesRegex(MonitorError, 'SSH 登录失败') as raised:
            service.connect('a', DEFAULT_SERVER, 'isolated-secret')
        self.assertNotIn('isolated-secret', str(raised.exception))
        self.assertTrue(client.closed)
        self.assertEqual(service._sessions, {})

    def test_snapshot_only_fixed_commands_log_sftp_and_busy_guard(self):
        client = FakeClient()
        service = ServerMonitor(lambda: client)
        service.connect('a', DEFAULT_SERVER, 'isolated-secret')
        service._command = Mock(side_effect=[('0, GPU, 90, 100, 1000, 60, 30', 0),
                                             ('100 3600 98 5 python python train.py\n1 100 0 0 sh sh', 0), ('up 1 day', 0)])
        reader = Mock()
        reader.read.return_value = {'log': 'step=1 loss=0.25', 'truncated': False,
                                  'metrics': [{'step': 1, 'loss': .25}], 'reset': False, 'history_limited': False}
        service._sessions['a']['log_readers']["/tmp/run ';whoami;.log"] = reader
        result = service.snapshot('a', "/tmp/run ';whoami;.log")
        self.assertEqual(len(result['processes']), 1)
        self.assertEqual(result['metrics'], [{'step': 1, 'loss': .25}])
        self.assertEqual(service._command.call_count, 3)
        commands = [c.args[1] for c in service._command.call_args_list]
        self.assertFalse(any('whoami' in c for c in commands))
        self.assertNotIn('isolated-secret', json.dumps(result))
        service._sessions['a']['lock'].acquire()
        with self.assertRaisesRegex(MonitorError, '正在读取'): service.snapshot('a')
        service._sessions['a']['lock'].release()
        service.disconnect('a')

    def test_log_read_bounded_and_regular_files_only(self):
        raw = b'x' * (MAX_OUTPUT * 2) + b'\nstep=50 loss=0.2\n'
        stream = io.BytesIO(raw)
        stream.stat = lambda: Mock(st_mode=stat.S_IFREG, st_size=len(raw))
        sftp = Mock()
        sftp.__enter__ = Mock(return_value=sftp); sftp.__exit__ = Mock(return_value=False)
        sftp.open.return_value = stream
        client = Mock(); client.open_sftp.return_value = sftp
        text, truncated = ServerMonitor._log(client, '/tmp/train.log')
        self.assertTrue(truncated)
        self.assertEqual(text, 'step=50 loss=0.2')
        stream = io.BytesIO(b'no'); stream.stat = lambda: Mock(st_mode=stat.S_IFIFO, st_size=2)
        sftp.open.return_value = stream
        with self.assertRaises(MonitorError): ServerMonitor._log(client, '/tmp/train.log')

    def test_command_timeout_and_output_limit_close_channel(self):
        client = Mock(); stdin = Mock(); stdout = Mock(); stderr = Mock()
        client.exec_command.return_value = stdin, stdout, stderr
        channel = stdout.channel
        channel.recv_ready.return_value = True
        channel.recv_stderr_ready.return_value = False
        channel.recv.return_value = b'x' * (MAX_OUTPUT + 1)
        channel.exit_status_ready.return_value = False
        with self.assertRaises(MonitorError): ServerMonitor()._command(client, 'uptime')
        channel.close.assert_called_once()
        tick = iter([0, 9])
        channel.reset_mock(); channel.recv_ready.return_value = False
        with self.assertRaises(TimeoutError): ServerMonitor(clock=lambda: next(tick))._command(client, 'uptime')
        channel.close.assert_called_once()


class MonitorRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='monitor-isolated-')
        self.app = create_app(self.tmp.name, testing=True)
        self.client = self.app.test_client(); self.client.get('/setup')
        self.post('/setup', password='test-owner-password', confirm='test-owner-password')
        self.client.get('/')
        self.service = ServerMonitor(FakeClient)
        self.app.extensions['server_monitor'] = self.service

    def tearDown(self):
        for token in list(self.service._sessions): self.service.disconnect(token)
        self.tmp.cleanup()

    def post(self, path, **fields):
        with self.client.session_transaction() as s: token = s['csrf']
        return self.client.post(path, data={'csrf': token, **fields})

    def test_auth_csrf_no_json_shortcut_or_embedded_password(self):
        self.assertEqual(self.app.test_client().get('/monitor').location, '/login')
        self.assertEqual(self.client.post('/api/monitor/connect', data=DEFAULT_SERVER).status_code, 400)
        self.assertEqual(self.client.post('/api/monitor/server', json={'action': 'gpu'}).status_code, 400)
        page = self.client.get('/monitor')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'isolated-secret', page.data)
        self.assertEqual(self.post('/api/monitor/server').status_code, 401)

    def test_connect_memory_only_settings_and_logout_erasure(self):
        response = self.post('/api/monitor/connect', **DEFAULT_SERVER, password='isolated-secret')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('isolated-secret', response.get_data(as_text=True))
        path = Path(self.tmp.name) / 'monitor-server.json'
        self.assertEqual(json.loads(path.read_text()), DEFAULT_SERVER)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'isolated-secret', (Path(self.tmp.name) / 'research.sqlite3').read_bytes())
        self.assertNotIn('isolated-secret', str(self.client.get('/monitor').headers))
        self.assertEqual(len(self.service._sessions), 1)
        self.post('/logout'); self.assertEqual(self.service._sessions, {})

    def test_snapshot_error_input_and_disconnect(self):
        self.post('/api/monitor/connect', **DEFAULT_SERVER, password='isolated-secret')
        result = self.post('/api/monitor/server', log_path='/tmp/../secret').json
        self.assertIn('绝对路径', result['error'])
        self.post('/api/monitor/disconnect')
        self.assertEqual(self.service._sessions, {})

    def test_remote_plain_http_cannot_send_password(self):
        with self.client.session_transaction() as s: csrf = s['csrf']
        response = self.client.post('/api/monitor/connect', data={'csrf': csrf, **DEFAULT_SERVER, 'password': 'isolated-secret'},
                                    environ_overrides={'REMOTE_ADDR': '192.168.1.10'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service._sessions, {})

    def test_query_does_not_hold_global_workspace_lock(self):
        self.post('/api/monitor/connect', **DEFAULT_SERVER, password='isolated-secret')
        def snapshot(*args):
            from flask import g
            self.assertFalse(g.get('workspace_locked', False))
            return {'gpus': [], 'processes': [], 'metrics': [], 'warnings': []}
        self.service.snapshot = snapshot
        self.assertEqual(self.post('/api/monitor/server').status_code, 200)


if __name__ == '__main__':
    unittest.main()
