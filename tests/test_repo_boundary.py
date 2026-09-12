import unittest
from tools.check_repo_boundary import inspect_blob

class RepoBoundaryTests(unittest.TestCase):
    def test_customer_files_are_rejected_even_if_forced_staged(self):
        for name in ('orders/demo/workflow.json','.private/wechat/export.json','config/runtime.json','assets/deck.pptx'):
            with self.subTest(name=name):self.assertTrue(inspect_blob(name,b'{}'))

    def test_token_patterns_are_rejected_without_echoing_value(self):
        token=b'ghp_'+b'a'*36
        issues=inspect_blob('src/settings.py',token)
        self.assertIn('GitHub令牌',issues)
        self.assertNotIn(token.decode(),str(issues))

    def test_normal_code_and_documentation_are_allowed(self):
        self.assertEqual(inspect_blob('docs/example.md','原文和密钥留本地'.encode()),[])
        self.assertEqual(inspect_blob('adapters/wechat_read.py',b"KEYS = TOOLS / 'wechat_keys.json'"),[])
