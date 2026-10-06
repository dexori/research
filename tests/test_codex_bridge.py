import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_bridge import BridgeError, CodexBridge


class CodexBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-bridge-test-')
        self.bridge = CodexBridge(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

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


if __name__ == '__main__':
    unittest.main()
