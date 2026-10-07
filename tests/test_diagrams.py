import tempfile
import unittest
from pathlib import Path

from app import create_app
from codex_bridge import INSTRUCTIONS, LEARNING_INSTRUCTIONS, SOURCED_LEARNING_INSTRUCTIONS


class DiagramTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='research-diagrams-test-')
        self.app=create_app(self.temp.name,testing=True)
        self.client=self.app.test_client()
        self.client.get('/setup')
        with self.client.session_transaction() as session:
            token=session['csrf']
        self.client.post('/setup',data={'csrf':token,'password':'diagram-test-password','confirm':'diagram-test-password'})

    def tearDown(self):
        self.temp.cleanup()

    def test_rendering_keeps_code_source_escaped_and_math_outside_code(self):
        html=str(self.app.jinja_env.filters['markdown']('```mermaid\nflowchart TD\nA["<script>alert(1)</script> $z$"] --> B["输出"]\n```\n\n$x=y$'))
        self.assertIn('class="language-mermaid"',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
        self.assertEqual(html.count('class="arithmatex"'),1)
        self.assertIn('$z$',html)
        self.assertNotIn('<svg',html)

    def test_renderer_is_authenticated_and_isolated_not_a_policy_change_for_notes(self):
        self.assertEqual(self.app.test_client().get('/diagrams/renderer').location,'/login')
        response=self.client.get('/diagrams/renderer')
        self.assertEqual(response.status_code,200)
        policy=response.headers['Content-Security-Policy']
        for setting in ["sandbox allow-scripts","connect-src 'none'","img-src 'none'","default-src 'none'","script-src 'self'"]:
            self.assertIn(setting,policy)
        self.assertNotIn('allow-same-origin',policy)
        self.assertIn('mermaid.tiny.js',response.text)
        self.assertIn('diagram_renderer.js',response.text)
        main=self.client.get('/assistant')
        self.assertIn("style-src 'self'",main.headers['Content-Security-Policy'])
        self.assertEqual(main.headers['X-Frame-Options'],'DENY')
        self.assertIn('diagrams.js',main.text)

    def test_vendor_is_local_and_prompts_do_not_claim_original_paper_images(self):
        with self.client.get('/static/vendor/mermaid/mermaid.tiny.js') as response:
            self.assertEqual(response.status_code,200)
        self.assertTrue((Path(__file__).resolve().parents[1]/'static/vendor/mermaid/LICENSE').is_file())
        for prompt in [INSTRUCTIONS,LEARNING_INSTRUCTIONS,SOURCED_LEARNING_INSTRUCTIONS]:
            self.assertIn('mermaid',prompt)
            self.assertIn('论文原图',prompt)
