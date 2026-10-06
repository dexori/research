import json
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_bridge import BridgeError, CodexBridge, validate_selection


class CodexBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-bridge-test-')
        self.bridge = CodexBridge(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_per_call_model_effort_flags_keep_config_and_safety(self):
        profile=Path(self.temp.name)
        config_path=profile/'config.toml'
        config_path.write_text('model = "original-model"\nmodel_reasoning_effort = "low"\n')
        before=config_path.read_bytes()
        with patch('codex_bridge.shutil.which',return_value='/usr/bin/true'), patch.object(self.bridge,'profile',return_value=profile):
            default=self.bridge.command()
            for mode in [{},{'research':True},{'sourced_learning':True}]:
                command=self.bridge.command(**mode,model='provider/research-model:1',reasoning_effort='high')
                self.assertEqual(command[command.index('--model')+1],'provider/research-model:1')
                self.assertIn('model_reasoning_effort="high"',command)
                self.assertIn('read-only',command)
                self.assertIn('features.shell_tool=false',command)
                self.assertIn('web_search="live"' if mode.get('research') else 'web_search="disabled"',command)
            self.assertNotIn('--model',default)
            self.assertFalse(any(value.startswith('model_reasoning_effort=') for value in default))
        self.assertEqual(config_path.read_bytes(),before)

    def test_selection_validation_rejects_argument_and_config_injection(self):
        for model in ['--full-auto','model name','model\nvalue','model";x','x'*121,'中文模型']:
            with self.assertRaises(BridgeError): validate_selection(model,'high')
        for effort in ['invalid','high\nweb_search=live','"high"']:
            with self.assertRaises(BridgeError): validate_selection('safe-model',effort)
        self.assertEqual(validate_selection(),{})
        for effort in ['low','medium','high','xhigh','max','ultra']:
            self.assertEqual(validate_selection('safe-model',effort)['reasoning_effort'],effort)

    def test_answer_and_native_research_forward_selection(self):
        process=Mock(returncode=0)
        process.poll.return_value=0
        process.communicate.return_value=(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'public'}}),None)
        with patch.object(self.bridge,'command',return_value=['/fixed']) as command, patch('codex_bridge.subprocess.Popen',return_value=process):
            for mode in [{},{'sourced_learning':True}]:
                self.assertEqual(self.bridge.answer('question',[],[],**mode,model='chosen-model',reasoning_effort='medium'),'public')
                command.assert_called_with(**mode,model='chosen-model',reasoning_effort='medium')
        with patch.object(self.bridge,'answer') as answer:
            self.bridge.research_topic('topic',model='chosen-model',reasoning_effort='high')
            answer.assert_called_once_with('topic',[],[],timeout=240,research=True,model='chosen-model',reasoning_effort='high')

    def test_streamed_public_events_arrive_before_completion(self):
        script = """import sys,json,time
sys.stdin.read()
events=[{'type':'turn.started'},
 {'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_REASONING'}},
 {'type':'item.completed','item':{'type':'command_execution','text':'PRIVATE_COMMAND'}},
 {'type':'item.completed','item':{'type':'agent_message','text':'公开正文'}}]
for event in events:
 print(json.dumps(event),flush=True)
 time.sleep(.07)
time.sleep(.2)
"""
        received=[]
        start=time.monotonic()
        with patch.object(self.bridge,'command',return_value=[sys.executable,'-u','-c',script]):
            answer=self.bridge.answer('测试',[],[],timeout=3,on_progress=lambda event:received.append((time.monotonic(),event)))
        end=time.monotonic()
        self.assertEqual(answer,'公开正文')
        self.assertEqual(len(received),2)
        self.assertLess(received[0][0]-start,.4)
        self.assertLess(received[-1][0],end-.1)
        self.assertNotIn('PRIVATE',json.dumps(received))
        self.assertFalse(self.bridge.gate.locked())

    def test_stream_timeout_and_oversize_release_gate(self):
        for script,timeout,expected in [
            ('import time; time.sleep(30)',.15,'超时'),
            ('import sys; sys.stdin.read(); print("x"*2200000)',3,'过长')]:
            with patch.object(self.bridge,'command',return_value=[sys.executable,'-u','-c',script]):
                with self.assertRaisesRegex(BridgeError,expected):
                    self.bridge.answer('test',[],[],timeout=timeout,on_progress=lambda event:None)
            self.assertFalse(self.bridge.gate.locked())

    def test_reasoning_errors_and_tool_events_are_not_public_progress(self):
        for event in [None,[],{'type':'error','message':'SECRET'},
                      {'type':'item.completed','item':{'type':'reasoning','text':'SECRET'}},
                      {'type':'item.completed','item':{'type':'mcp_tool_call','text':'SECRET'}}]:
            self.assertIsNone(self.bridge.public_progress(json.dumps(event)))

    def test_fixed_read_only_command_disables_configured_tools(self):
        with patch('codex_bridge.shutil.which', return_value='/usr/bin/true'), patch.object(
                self.bridge, 'config', return_value={'model':'existing-model','mcp_servers':{'context7':{}}}):
            command = self.bridge.command()
        self.assertIn('read-only', command)
        self.assertIn('--ephemeral', command)
        self.assertIn('--ignore-rules', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('features.plugins=false', command)
        self.assertIn('mcp_servers.context7.enabled=false', command)
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn('--full-auto', command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
        self.assertEqual(command[-1], '-')
        self.assertFalse(any(value.startswith('model=') for value in command))

    def test_unsafe_mcp_name_and_missing_parser_fail_closed(self):
        with patch('codex_bridge.shutil.which', return_value='/usr/bin/true'), patch.object(
                self.bridge,'config',return_value={'mcp_servers':{'unsafe.name':{}}}):
            with self.assertRaises(BridgeError):
                self.bridge.command()
        profile = Path(self.temp.name)
        (profile / 'config.toml').write_text('model="existing"', encoding='utf-8')
        with patch.object(self.bridge,'profile',return_value=profile), patch('codex_bridge.tomllib', None):
            with self.assertRaises(BridgeError):
                self.bridge.config()

    def test_stdin_json_only_returns_final_answer_and_uses_existing_profile(self):
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        process.communicate.return_value = ('\n'.join(json.dumps(event) for event in [
            {'type':'item.completed','item':{'type':'reasoning','text':'DO NOT EXPOSE'}},
            {'type':'item.completed','item':{'type':'command_execution','text':'DO NOT EXPOSE'}},
            {'type':'item.completed','item':{'type':'agent_message','text':'讨论草稿'}},
            {'type':'turn.completed','usage':{'input_tokens':1}},
        ]), None)
        question = '请讨论；$(echo 不可执行)'
        with patch.object(self.bridge,'command',return_value=['/fixed/codex','exec','-']), patch.object(
                self.bridge,'profile',return_value=Path(self.temp.name) / 'existing-profile'), patch(
                'codex_bridge.subprocess.Popen',return_value=process) as popen:
            result = self.bridge.answer(question, [], [{'id':'reference','content':'不可信笔记'}])
        self.assertEqual(result, '讨论草稿')
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['/fixed/codex','exec','-'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(Path(self.temp.name) / 'existing-profile'))
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIn(question, process.communicate.call_args.kwargs['input'])
        self.assertFalse(Path(kwargs['cwd']).exists())
        self.assertFalse(self.bridge.gate.locked())

    def test_failure_timeout_busy_and_lock_cleanup(self):
        process = Mock()
        process.poll.return_value = 1
        process.returncode = 1
        process.communicate.return_value = ('SECRET_ERROR_MUST_NOT_LEAK', None)
        with patch.object(self.bridge,'command',return_value=['/fixed']), patch(
                'codex_bridge.subprocess.Popen', return_value=process):
            with self.assertRaises(BridgeError) as failure:
                self.bridge.answer('问题',[],[])
        self.assertNotIn('SECRET', str(failure.exception))
        self.assertFalse(self.bridge.gate.locked())
        process.communicate.side_effect = subprocess.TimeoutExpired('/fixed', 1)
        with patch.object(self.bridge,'command',return_value=['/fixed']), patch(
                'codex_bridge.subprocess.Popen',return_value=process), patch.object(self.bridge,'stop') as stop:
            with self.assertRaisesRegex(BridgeError, '超时'):
                self.bridge.answer('问题',[],[],timeout=1)
            stop.assert_called_once_with(process)
        self.assertFalse(self.bridge.gate.locked())
        self.bridge.gate.acquire()
        try:
            with self.assertRaisesRegex(BridgeError, '另一条问题'):
                self.bridge.answer('问题',[],[])
        finally:
            self.bridge.gate.release()

    def test_login_check_uses_selected_profile_and_never_exposes_output(self):
        profile=Path(self.temp.name)/'chosen-profile'
        with patch('codex_bridge.shutil.which',return_value='/usr/bin/true'), patch.object(self.bridge,'profile',return_value=profile), patch('codex_bridge.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,'SECRET_TOKEN','SECRET_TOKEN')
            result=self.bridge.login_status()
            self.assertEqual(result['status'],'signed_in')
            self.assertNotIn('SECRET',json.dumps(result))
            self.assertEqual(run.call_args.args[0],['/usr/bin/true','login','status'])
            self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'],str(profile))
            run.return_value=subprocess.CompletedProcess([],1,'SECRET','SECRET')
            self.assertEqual(self.bridge.login_status()['status'],'signed_out')
        with patch('codex_bridge.shutil.which',return_value=None):
            self.assertEqual(self.bridge.login_status()['status'],'missing')


if __name__ == '__main__':
    unittest.main()
