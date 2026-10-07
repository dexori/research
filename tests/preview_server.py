"""Isolated browser-check server. Never touches the user's data directory."""
import sys
import argparse
import io
import os
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from waitress import serve
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument('--fake-codex', action='store_true', help='Isolated deterministic browser fixture; no model calls')
args = parser.parse_args()

class PreviewCodex:
    def generate_image(self,prompt,on_progress=None,**selection):
        if on_progress:
            on_progress({'stage':'illustrating','detail':'隔离测试：等待图片生成事件。'})
            time.sleep(2)
        if os.environ.get('TEST_IMAGE_PATH'):
            return Path(os.environ['TEST_IMAGE_PATH']).read_bytes()
        output=io.BytesIO();Image.new('RGB',(640,360),'#d9ede2').save(output,format='PNG');return output.getvalue()
    def research_topic(self, topic, on_progress=None, **selection):
        return {'content':'# ACT 学习卡\n\n分类：模型架构\n标签：ACT, CVAE, action chunking\n\n## 一句话理解\n\n隔离测试知识卡，不是实际检索结果。\n\n## 核心原理与数据流\n\n隔离测试示意：观测、动作块与执行；请核对原文。\n\n```mermaid\nflowchart LR\nA["观测"] --> B["动作块"]\n```\n\n## 最小例子\n\n隔离公式测试，并非 ACT 的原论文目标：\n\n$$L = \\sum_i d_i$$\n\n## 来源与阅读顺序\n\n[原论文](https://arxiv.org/abs/2304.13705)\n\n[作者项目](https://tonyzhaozh.github.io/aloha/)', 'search_count':2}
    def login_status(self):
        return {'status':'signed_in','message':'隔离测试：CLI 报告已登录；没有调用模型。'}
    def status(self):
        return {'available':True,'profile':'隔离测试终端','model':'浏览器测试模型','read_only':True}

    def answer(self, question, history, sources, on_progress=None, **selection):
        if on_progress:
            on_progress({'stage':'generating','detail':'隔离测试：终端开始回答。'})
            time.sleep(1.5)
            on_progress({'stage':'generating','detail':'隔离测试：收到公开回复。','output':'第一段公开回复：待验证的研究假设。'})
            time.sleep(1.5)
        return '## 研究讨论草稿\n\n**待验证的假设**，不是已证实的实验结论。\n\n行内公式：$z_t = f_\\theta(o_t)$。\n\n$$\nL = \\sum_{i=1}^{n} d_i\n$$\n\n' + (
            f"参考 [{sources[0]['title']}](/entry/{sources[0]['id']})" if sources else '未检索到相关资料。') + '\n\n```mermaid\nflowchart LR\nA["研究问题"] --> B["对照实验"] --> C["待验证结果"]\n```\n\n隔离测试请求设置：'+str(selection)

with tempfile.TemporaryDirectory(prefix="research-browser-") as directory:
    print('Browser fixture data: '+directory,flush=True)
    app = create_app(directory, testing=True)
    if args.fake_codex:
        app.extensions['codex_bridge'] = PreviewCodex()
        class PreviewLearning:
            def research(self,topic,bridge,on_progress=None,**selection):
                if on_progress:
                    sources=[{'title':'ACT 原论文（隔离测试）','url':'https://arxiv.org/abs/2304.13705','scope':'隔离测试正文'}]
                    for stage,detail in [('planning','隔离测试：确定主题'),('searching','隔离测试：已找到原论文'),
                                         ('reading','隔离测试：读取正文'),('generating','隔离测试：生成知识卡')]:
                        on_progress({'stage':stage,'detail':detail,'sources':sources})
                        time.sleep(1.5)
                return bridge.research_topic(topic,**selection)
        app.extensions['learning_researcher'] = PreviewLearning()
        class PreviewPaper:
            def read(self, **kwargs):
                return {'text':'隔离测试论文正文：方法、证据与局限。'*80,'method':'隔离 PDF 测试','pages':2,'pages_read':2,'truncated':False}
        app.extensions['paper_reader'] = PreviewPaper()
    serve(app, host="127.0.0.1", port=8766)
