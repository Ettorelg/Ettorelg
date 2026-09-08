"""Test isolati del trasporto email, senza database e senza invii reali."""
import ast
import os
import re
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class EmailProviderTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        names = {"smtp_configured", "email_configured", "valid_email_address", "send_transactional_email"}
        module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        self.http = SimpleNamespace(post=Mock(), RequestException=ConnectionError)
        self.scope = {"os": os, "re": re, "requests": self.http, "app": SimpleNamespace(logger=Mock())}
        exec(compile(module, "app.py", "exec"), self.scope)
        self.env = patch.dict(os.environ, {"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "test-key", "EMAIL_FROM": "Menu <menu@example.com>"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def send(self):
        return self.scope["send_transactional_email"]("test@example.com", "Test", "Body", "support@example.com")

    def test_success(self):
        self.http.post.return_value = Mock(status_code=200, json=Mock(return_value={"id": "test-id"}))
        self.assertTrue(self.send())
        args = self.http.post.call_args
        self.assertEqual(args.args[0], "https://api.resend.com/emails")
        self.assertEqual(args.kwargs["json"]["reply_to"], "support@example.com")
        self.assertEqual(args.kwargs["json"]["text"], "Body")

    def test_rejection(self):
        self.http.post.return_value = Mock(status_code=403)
        self.assertFalse(self.send())

    def test_network_failure(self):
        self.http.post.side_effect = ConnectionError()
        self.assertFalse(self.send())

    def test_missing_configuration(self):
        del os.environ["RESEND_API_KEY"]
        self.assertFalse(self.send())
        self.http.post.assert_not_called()

    def test_unknown_provider(self):
        os.environ["EMAIL_PROVIDER"] = "invalid"
        self.assertFalse(self.send())

    def test_invalid_reply_to_is_omitted(self):
        self.http.post.return_value = Mock(status_code=200, json=Mock(return_value={"id": "test-id"}))
        self.assertTrue(self.scope["send_transactional_email"]("test@example.com", "Test", "Body", "2@2.2"))
        self.assertNotIn("reply_to", self.http.post.call_args.kwargs["json"])


if __name__ == "__main__":
    unittest.main()
