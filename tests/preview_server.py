"""Isolated browser-check server. Never touches the user's data directory."""
import sys
import argparse
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from waitress import serve

parser = argparse.ArgumentParser()
parser.add_argument('--fake-codex', action='store_true', help='Isolated deterministic browser fixture; no model calls')
args = parser.parse_args()

class PreviewCodex:
    def status(self):
        return {'available':True,'profile':'隔离测试终端','model':'浏览器测试模型','read_only':True}

    def answer(self, question, history, sources):
        return '## 研究讨论草稿\n\n**待验证的假设**，不是已证实的实验结论。\n\n' + (
            f"参考 [{sources[0]['title']}](/entry/{sources[0]['id']})" if sources else '未检索到相关资料。')

with tempfile.TemporaryDirectory(prefix="research-browser-") as directory:
    app = create_app(directory, testing=True)
    if args.fake_codex:
        app.extensions['codex_bridge'] = PreviewCodex()
    serve(app, host="127.0.0.1", port=8766)
