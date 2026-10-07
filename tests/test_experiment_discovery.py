import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from app import create_app
from draft_import import validate
from experiment_discovery import normalize_run, draft_values
from monitoring import DEFAULT_SERVER, MonitorError, ServerMonitor
from remote_training_probe import flags, flatten, yaml_values, training_command, read_config, inspect_run, startup_config
from test_monitoring import FakeClient


def sample():
    return {'pid': '1234', 'identity': 'a'*64, 'worker_count': 4, 'script': 'train.py', 'cwd': '/tmp/training',
            'fields': {'model': 'SmolVLA', 'dataset': 'LIBERO', 'seed': '0', 'learning_rate': '1e-4',
                       'batch_size': '32', 'total_steps': '70000', 'log_path': '/tmp/training/train.log'},
            'sources': {'model': '启动参数', 'learning_rate': '配置：/tmp/config.json'},
            'extras': {'contrastive_weight': '0.1', 'api_token': 'isolated-secret'}}


class ProbeTest(unittest.TestCase):
    def test_effective_startup_config_custom_wrapper_and_no_code_execution(self):
        with tempfile.TemporaryDirectory(prefix='probe-startup-') as tmp:
            root = Path(tmp); log = root/'train.log'
            values = {'accelerator': {}, 'policy': {'type':'pi05'}, 'dataset': {'repo_id':'lerobot/libero'},
                      'optimizer':{'lr':2.5e-5}, 'steps':30000, 'stop_after_steps':3000, 'batch_size':16, 'seed':1000,
                      'api_token':'isolated-secret'}
            log.write_text('INFO startup '+repr(values)+'\nstep=1 loss=0.5')
            proc = {'pid':1234,'start':'555','cwd':root,
                    'argv':['torchrun','--module','examples.train_pilot','--variant=multi','--log_path='+str(log)]}
            result = inspect_run(proc,[1234],'boot')
            self.assertEqual(result['fields']['learning_rate'],'2.5e-05')
            self.assertEqual(result['fields']['total_steps'],'3000')
            self.assertEqual(result['fields']['model'],'pi05')
            self.assertIn('日志启动配置',result['sources']['model'])
            self.assertEqual(result['script'],'examples.train_pilot')
            self.assertNotIn('isolated-secret',json.dumps(result))
            log.write_text('INFO '+repr(values).replace("'seed': 1000", "'seed': 1000, 'output_dir': PosixPath('/tmp/run')"))
            self.assertEqual(startup_config(log)['output_dir'],'/tmp/run')
            log.write_text('INFO '+repr(values).replace("'accelerator': {}", "'accelerator': {'precision': <Precision.BF16: 'bf16'>}"))
            self.assertEqual(startup_config(log)['optimizer.lr'],'2.5e-05')
            log.write_text("{'batch_size': __import__('os').system('false'), 'policy': {}, 'steps': 1}")
            self.assertEqual(startup_config(log),{})
            log.write_text("{'step': 2, 'loss': 0.3}")
            self.assertEqual(startup_config(log),{})

    def test_cli_nested_config_yaml_and_secret_allowlist(self):
        cli = flags(['python','train.py','--policy.type=smolvla','--batch-size','32','--api-token=isolated-secret','--seed=0'])
        self.assertEqual(cli['policy.type'], 'smolvla'); self.assertEqual(cli['batch_size'], '32')
        self.assertNotIn('api_token', cli)
        flat = flatten({'policy': {'type': 'smolvla'}, 'optimizer': {'lr': .0001}, 'api_token': 'isolated-secret'})
        self.assertEqual(flat['policy.type'], 'smolvla'); self.assertNotIn('api_token', flat)
        yaml = yaml_values('policy:\n  type: smolvla\noptimizer:\n  lr: 1e-4\npassword: isolated-secret\nbad: !!python/object:thing\nunresolved: ${foo}')
        self.assertEqual(yaml['optimizer.lr'], '1e-4'); self.assertNotIn('password', yaml)
        self.assertNotIn('bad', yaml); self.assertNotIn('unresolved', yaml)

    def test_training_recognition_does_not_execute_or_classify_evaluation(self):
        for argv in [['python','train_smolvla.py'],['python','-m','lerobot.scripts.lerobot_train'],['torchrun','--nproc_per_node=4','train.py']]:
            self.assertTrue(training_command(argv))
        for argv in [['python','server.py'],['python','eval.py'],['python','-c','print("train.py")']]:
            self.assertFalse(training_command(argv))

    def test_actual_config_and_cli_override_with_provenance(self):
        with tempfile.TemporaryDirectory(prefix='probe-config-') as tmp:
            root = Path(tmp)
            config = root/'train_config.json'; log = root/'train.log'
            config.write_text(json.dumps({'policy': {'type':'smolvla'}, 'dataset':{'repo_id':'user/LIBERO'},
                                        'optimizer':{'lr':.0002}, 'batch_size':32, 'steps':70000, 'seed':0,
                                        'api_token':'isolated-secret'}))
            log.write_text('step=1 loss=0.3')
            proc = {'pid':1234,'start':'555','cwd':root,
                    'argv':['python','train.py','--config_path='+str(config),'--optimizer.lr=1e-4','--log_path='+str(log)]}
            result = inspect_run(proc,[1234],'boot')
            self.assertEqual(result['fields']['learning_rate'],'1e-4')
            self.assertEqual(result['sources']['learning_rate'],'启动参数')
            self.assertEqual(result['fields']['total_steps'],'70000')
            self.assertEqual(result['fields']['log_path'],str(log))
            self.assertNotIn('isolated-secret',json.dumps(result))
            self.assertNotIn('api_token',json.dumps(result))

    def test_normalize_never_invents_objective_and_filters_bad_values(self):
        run = sample(); run['fields'].update(batch_size='bad', learning_rate='NaN', log_path='../bad.log', password='isolated-secret')
        clean = normalize_run(run,DEFAULT_SERVER)
        self.assertNotIn('batch_size',clean['fields']); self.assertNotIn('learning_rate',clean['fields'])
        self.assertNotIn('log_path',clean['fields']); self.assertNotIn('password',clean['fields'])
        metadata, body = draft_values(clean,DEFAULT_SERVER)
        self.assertNotIn('objective',metadata); self.assertIn('待补充',body)
        self.assertNotIn('isolated-secret',json.dumps(metadata))

    def test_discovery_fixed_code_no_pid_shell_injection_and_close(self):
        monitor=ServerMonitor(FakeClient); monitor.connect('a',DEFAULT_SERVER,'isolated-secret')
        monitor._command=Mock(return_value=(json.dumps({'runs':[sample()]}),0))
        self.assertEqual(len(monitor.discover('a','1234')['runs']),1)
        command=monitor._command.call_args.args[1]
        self.assertTrue(command.startswith('python3 -I -c ')); self.assertTrue(command.endswith(' 1234'))
        with self.assertRaises(MonitorError): monitor.discover('a','1234;touch /tmp/bad')
        monitor.disconnect('a')


class DiscoveryRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='discovery-isolated-')
        self.app=create_app(self.tmp.name,testing=True)
        self.client=self.app.test_client(); self.client.get('/setup')
        self.post('/setup',password='test-owner-password',confirm='test-owner-password'); self.client.get('/')
        self.server=Mock(); self.server.state.return_value=DEFAULT_SERVER
        self.server.discover.return_value={'server':DEFAULT_SERVER,'runs':[sample()]}
        self.app.extensions['server_monitor']=self.server

    def tearDown(self): self.tmp.cleanup()
    def post(self,url,**fields):
        with self.client.session_transaction() as s: csrf=s['csrf']
        return self.client.post(url,data={'csrf':csrf,**fields})
    def counts(self):
        with closing(sqlite3.connect(Path(self.tmp.name)/'research.sqlite3')) as db:
            return db.execute('SELECT count(*) FROM entries').fetchone()[0]

    def test_discover_requires_auth_csrf_and_is_unsaved(self):
        self.assertEqual(self.app.test_client().post('/api/monitor/discover').location,'/login')
        self.assertEqual(self.client.post('/api/monitor/discover').status_code,400)
        result=self.post('/api/monitor/discover').json
        self.assertEqual(result['runs'][0]['fields']['batch_size'],'32')
        self.assertNotIn('isolated-secret',json.dumps(result)); self.assertEqual(self.counts(),0)

    def test_autofill_review_save_replay_and_known_experiment(self):
        run=self.post('/api/monitor/discover').json['runs'][0]
        first=self.post('/api/monitor/autofill',pid=run['pid'],identity=run['identity'])
        self.assertEqual(first.status_code,200)
        second=self.post('/api/monitor/autofill',pid=run['pid'],identity=run['identity'])
        self.assertEqual(first.json['review_url'],second.json['review_url']); self.assertEqual(self.counts(),0)
        review=self.client.get(first.json['review_url'])
        self.assertIn(b'value="32"',review.data); self.assertIn(b'value="1e-4"',review.data)
        identifier=first.json['review_url'].split('/imports/')[1].split('?')[0]
        saved=self.post('/new/experiment',import_id=identifier,title='auto SmolVLA',status='进行中',
                        model='SmolVLA',dataset='LIBERO',batch_size='32',learning_rate='1e-4',total_steps='70000',
                        training_pid='1234',log_path='/tmp/training/train.log',content='reviewed',return_monitor='1')
        self.assertEqual(saved.status_code,302); self.assertIn('/monitor?experiment=',saved.location)
        self.assertEqual(self.counts(),1)
        again=self.post('/api/monitor/autofill',pid=run['pid'],identity=run['identity'])
        self.assertTrue(again.json['existing']); self.assertEqual(self.counts(),1)
        markdown=next(Path(self.tmp.name).rglob('*.md')).read_text()
        self.assertIn('discovery_identity',markdown); self.assertNotIn('isolated-secret',markdown)

    def test_stopped_reused_pid_and_ssh_does_not_hold_workspace_lock(self):
        self.assertEqual(self.post('/api/monitor/autofill',pid='bad').status_code,400)
        self.assertEqual(self.post('/api/monitor/autofill',pid='1234',identity='wrong').status_code,409)
        self.server.discover.return_value={'server':DEFAULT_SERVER,'runs':[]}
        self.assertEqual(self.post('/api/monitor/autofill',pid='1234',identity='wrong').status_code,409)
        def discover(*args):
            from flask import g
            self.assertFalse(g.get('workspace_locked',False))
            return {'server':DEFAULT_SERVER,'runs':[]}
        self.server.discover.side_effect=discover
        self.assertEqual(self.post('/api/monitor/discover').status_code,200)


if __name__=='__main__': unittest.main()
