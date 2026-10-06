from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from desktop_launcher import probe, ensure_running, launch_environment, desktop_entry, LaunchError


class LauncherTest(unittest.TestCase):
    def test_probe_recognizes_platform_and_rejects_other_service(self):
        class Handler(BaseHTTPRequestHandler):
            marker = True
            def do_GET(self):
                self.send_response(200)
                if self.marker: self.send_header('X-Research-Hub','1')
                self.end_headers()
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        port=server.server_address[1]
        try:
            self.assertEqual(probe(port),'ready')
            Handler.marker=False
            self.assertEqual(probe(port),'occupied')
        finally:
            server.shutdown();server.server_close();thread.join()
        self.assertEqual(probe(port),'stopped')

    def test_existing_platform_never_starts_a_second_server(self):
        with tempfile.TemporaryDirectory() as root, patch('desktop_launcher.probe',return_value='ready'), patch('desktop_launcher.subprocess.Popen') as popen:
            self.assertFalse(ensure_running(root))
            popen.assert_not_called()

    def test_occupied_port_and_missing_dependencies_fail_without_launch(self):
        with tempfile.TemporaryDirectory() as root, patch('desktop_launcher.subprocess.Popen') as popen:
            with patch('desktop_launcher.probe',return_value='occupied'), self.assertRaises(LaunchError):
                ensure_running(root)
            with patch('desktop_launcher.probe',return_value='stopped'), self.assertRaisesRegex(LaunchError,'依赖'):
                ensure_running(root)
            popen.assert_not_called()

    def test_launch_is_detached_local_only_and_reuses_codex_binary_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'.venv/bin').mkdir(parents=True)
            (root/'.venv/bin/python').touch()
            (root/'data').mkdir()
            (root/'data/desktop-launcher.json').write_text(json.dumps({'RESEARCH_CODEX_BIN':'/private/bin/codex','SECRET':'must-not-copy'}))
            process=Mock();process.pid=999999;process.poll.return_value=None
            with patch.dict(os.environ,{},clear=True), patch('desktop_launcher.probe',side_effect=['stopped','ready']), patch('desktop_launcher.subprocess.Popen',return_value=process) as popen:
                self.assertTrue(ensure_running(root))
            args, kwargs=popen.call_args
            self.assertEqual(args[0],[str(root/'.venv/bin/python'),str(root/'app.py'),'--host','127.0.0.1','--port','8765'])
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(kwargs['env']['RESEARCH_CODEX_BIN'],'/private/bin/codex')
            self.assertNotIn('SECRET',kwargs['env'])
            self.assertNotIn('shell',kwargs)
            self.assertEqual((root/'data/desktop-server.log').stat().st_mode & 0o777,0o600)

    def test_environment_explicit_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'desktop-launcher.json').write_text(json.dumps({'RESEARCH_CODEX_BIN':'/old/codex','RESEARCH_CODEX_HOME':'/old/profile'}))
            with patch.dict(os.environ,{'RESEARCH_CODEX_BIN':'/chosen/codex'},clear=True):
                env=launch_environment(path)
            self.assertEqual(env['RESEARCH_CODEX_BIN'],'/chosen/codex')
            self.assertEqual(env['RESEARCH_CODEX_HOME'],'/old/profile')

    def test_desktop_entry_quotes_paths_and_never_runs_shell(self):
        entry=desktop_entry('/tmp/Research Hub % copy')
        self.assertIn('Name=研序科研工作台',entry)
        self.assertIn('"/tmp/Research Hub %% copy/.venv/bin/python"',entry)
        self.assertIn('Terminal=false',entry)
        self.assertNotIn('bash -c',entry)
        with self.assertRaises(LaunchError):desktop_entry('/tmp/invalid\npath')


if __name__=='__main__': unittest.main()
