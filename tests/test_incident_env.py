import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class EnvironmentScriptTests(unittest.TestCase):
    def test_fake_setup_writes_restricted_file_without_echoing_secrets(self):
        script = Path(__file__).parent.parent / "scripts" / "set-ukraine-incident-env.sh"
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, PROVENANCE_INCIDENT_FAKE="1")
            answers = "https://example.supabase.co\nfake-service-key-for-tests-only-0123456789\nfake-deepseek-key-for-tests-only-0123456789\n"
            result = subprocess.run(
                ["bash", str(script)], input=answers, text=True, capture_output=True, env=env, timeout=20
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = Path(home) / ".config" / "provenance-incident" / "ukraine.env"
            self.assertTrue(target.is_file())
            if os.name == "nt":
                acl = subprocess.run(["icacls", str(target)], capture_output=True, text=True, timeout=20).stdout
                current_user = os.environ.get("USERNAME") or subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
                self.assertIn(current_user, acl)
                self.assertIn(":(F)", acl)
                self.assertNotIn("Everyone", acl)
                self.assertNotIn("BUILTIN\\Users", acl)
            else:
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertIn("SUPABASE_URL=https://example.supabase.co", target.read_text())
            self.assertNotIn("fake-deepseek-key-for-tests-only-0123456789", result.stdout)
            self.assertNotIn("fake-service-key-for-tests-only-0123456789", result.stdout)

    def test_blank_deepseek_key_writes_config_without_model_secret(self):
        script = Path(__file__).parent.parent / "scripts" / "set-ukraine-incident-env.sh"
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, PROVENANCE_INCIDENT_FAKE="0")
            answers = "https://example.supabase.co\nfake-service-key-for-tests-only-0123456789\n\n"
            result = subprocess.run(
                ["bash", str(script)], input=answers, text=True, capture_output=True, env=env, timeout=20
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = Path(home) / ".config" / "provenance-incident" / "ukraine.env"
            contents = target.read_text()
            self.assertIn("DEEPSEEK_KEY_UKRAINE=\n", contents)
            self.assertNotIn("fake-deepseek-key-for-tests-only-0123456789", contents)
            self.assertNotIn("fake-service-key-for-tests-only-0123456789", result.stdout + result.stderr)

    def test_short_deepseek_key_is_rejected(self):
        script = Path(__file__).parent.parent / "scripts" / "set-ukraine-incident-env.sh"
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, PROVENANCE_INCIDENT_FAKE="0")
            answers = "https://example.supabase.co\nfake-service-key-for-tests-only-0123456789\nshort\n"
            result = subprocess.run(
                ["bash", str(script)], input=answers, text=True, capture_output=True, env=env, timeout=20
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("DEEPSEEK_KEY_UKRAINE", result.stderr)
            target = Path(home) / ".config" / "provenance-incident" / "ukraine.env"
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
