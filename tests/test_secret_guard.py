"""Offline regression tests for local Git security hooks."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "secret_guard.py"


def run(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, input=stdin, capture_output=True, text=True, check=False)


class SecretGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.repo = Path(self.t.name)
        assert run(self.repo, "git", "init", "-q").returncode == 0
        run(self.repo, "git", "config", "user.email", "security-test@example.invalid")
        run(self.repo, "git", "config", "user.name", "Offline Security Test")

    def write(self, path: str, content: str) -> None:
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def guard(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return run(self.repo, sys.executable, str(GUARD), *args, stdin=stdin)

    def test_safe_example_and_safe_code_pass(self):
        self.write(".env.example", "API_KEY=\n")
        self.write("app.py", "API_KEY = os.getenv('API_KEY')\n")
        run(self.repo, "git", "add", ".")
        self.assertEqual(self.guard("--staged").returncode, 0)

    def test_force_staged_dotenv_is_blocked(self):
        self.write(".env", "API_KEY=\n")
        run(self.repo, "git", "add", "-f", ".env")
        r = self.guard("--staged")
        self.assertEqual(r.returncode, 1)
        self.assertIn(".env: sensitive filename", r.stderr)

    def test_dotenv_production_in_nested_directory_blocked(self):
        self.write("config/.env.production", "NOT_A_SECRET=1\n")
        run(self.repo, "git", "add", "-f", "config/.env.production")
        self.assertEqual(self.guard("--staged").returncode, 1)

    def test_plaintext_token_in_source_blocked_without_value_echo(self):
        fake = "github_pat_" + "A" * 25
        self.write("app.py", "token = '" + fake + "'\n")
        run(self.repo, "git", "add", "app.py")
        r = self.guard("--staged")
        self.assertEqual(r.returncode, 1)
        self.assertIn("provider token", r.stderr)
        self.assertNotIn(fake, r.stderr)

    def test_private_key_blocked(self):
        self.write("public.txt", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")
        run(self.repo, "git", "add", ".")
        self.assertEqual(self.guard("--staged").returncode, 1)

    def test_generic_api_key_assignment_blocked(self):
        self.write("settings.py", 'API_KEY = "aPrivateKeyStringNotForGit"\n')
        run(self.repo, "git", "add", ".")
        self.assertEqual(self.guard("--staged").returncode, 1)

    def test_push_history_catches_deleted_secret(self):
        self.write("README.md", "safe\n")
        run(self.repo, "git", "add", ".")
        assert run(self.repo, "git", "commit", "-qm", "safe").returncode == 0
        base = run(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        self.write(".env", "SECRET=never\n")
        run(self.repo, "git", "add", "-f", ".env")
        assert run(self.repo, "git", "commit", "-qm", "oops").returncode == 0
        run(self.repo, "git", "rm", "-q", ".env")
        assert run(self.repo, "git", "commit", "-qm", "delete").returncode == 0
        head = run(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        push_info = f"refs/heads/main {head} refs/heads/main {base}\n"
        r = self.guard("--pre-push", stdin=push_info)
        self.assertEqual(r.returncode, 1)
        self.assertIn(".env", r.stderr)

    def test_new_branch_scan_all_reachable_history(self):
        self.write(".env", "SECRET=never\n")
        run(self.repo, "git", "add", "-f", ".env")
        assert run(self.repo, "git", "commit", "-qm", "oops").returncode == 0
        run(self.repo, "git", "rm", "-q", ".env")
        assert run(self.repo, "git", "commit", "-qm", "delete").returncode == 0
        head = run(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        push_info = f"refs/heads/main {head} refs/heads/main {'0'*40}\n"
        self.assertEqual(self.guard("--pre-push", stdin=push_info).returncode, 1)

    def commit(self, message: str) -> None:
        run(self.repo, "git", "add", "-A", "-f")
        assert run(self.repo, "git", "commit", "-qm", message).returncode == 0

    def test_exchange_secret_and_passphrase_assignments_are_blocked(self):
        # Re-audit: this app's own secret names passed the scan.
        for line in ('BINANCE_API_SECRET="Zq8vRealLookingSecret42"\n', "OKX_PASSPHRASE=RealLookingPass9921\n",
                     "api_secret: Zq8vRealLookingSecret42\n"):
            with self.subTest(line=line.split("=")[0].split(":")[0]):
                self.write("config.txt", line)
                run(self.repo, "git", "add", ".")
                r = self.guard("--staged")
                self.assertEqual(r.returncode, 1)
                self.assertNotIn("RealLooking", r.stderr + r.stdout)

    def test_history_catches_a_symlink_replaced_by_a_file_with_a_secret(self):
        (self.repo / "creds").symlink_to("elsewhere")
        self.commit("link")
        (self.repo / "creds").unlink()
        self.write("creds", "AKIA" + "C" * 16 + "\n")
        self.commit("type change")  # git records T (type change), which the scan skipped
        r = self.guard("--history")
        self.assertEqual(r.returncode, 1)
        self.assertIn("AWS access key", r.stderr)

    def test_history_clean_repository_passes(self):
        self.write("README.md", "safe\n")
        self.write("app.py", "API_KEY = os.getenv('API_KEY')\n")
        self.commit("safe")
        r = self.guard("--history")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASS", r.stdout)

    def test_history_catches_token_deleted_long_ago_without_value_echo(self):
        fake = "ghp_" + "B" * 30
        self.write("notes.txt", "token " + fake + "\n")
        self.commit("oops")
        self.write("notes.txt", "clean now\n")
        self.commit("scrub")
        self.write("README.md", "later work\n")
        self.commit("later")
        r = self.guard("--history")
        self.assertEqual(r.returncode, 1)
        self.assertIn("notes.txt: provider token", r.stderr)
        self.assertNotIn(fake, r.stderr + r.stdout)

    def test_history_catches_sensitive_filename_with_reused_blob(self):
        # Same content under a safe name and a sensitive name: the name check must still run.
        self.write("config.example", "NOT_A_SECRET=1\n")
        self.commit("example")
        self.write(".env", "NOT_A_SECRET=1\n")
        self.commit("oops")
        r = self.guard("--history")
        self.assertEqual(r.returncode, 1)
        self.assertIn(".env: sensitive filename", r.stderr)

    def test_history_catches_secret_introduced_by_merge_commit(self):
        self.write("README.md", "base\n")
        self.commit("base")
        main = run(self.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        run(self.repo, "git", "checkout", "-qb", "side")
        self.write("side.txt", "side\n")
        self.commit("side")
        run(self.repo, "git", "checkout", "-q", main)
        self.write("main.txt", "main\n")
        self.commit("main")
        assert run(self.repo, "git", "merge", "-q", "--no-commit", "--no-ff", "side").returncode == 0
        self.write("merge.py", 'CLIENT_SECRET = "aMergeOnlyCredentialValue"\n')
        self.commit("merge with an extra file")
        r = self.guard("--history")
        self.assertEqual(r.returncode, 1)
        self.assertIn("merge.py", r.stderr)
        self.assertNotIn("aMergeOnlyCredentialValue", r.stderr)

    def test_history_refuses_shallow_clone(self):
        self.write("README.md", "one\n")
        self.commit("one")
        self.write("README.md", "two\n")
        self.commit("two")
        clone = self.repo / "shallow"
        assert run(self.repo, "git", "clone", "-q", "--depth", "1", "file://" + str(self.repo), str(clone)).returncode == 0
        r = run(clone, sys.executable, str(GUARD), "--history")
        self.assertEqual(r.returncode, 2)
        self.assertIn("shallow", r.stderr)


if __name__ == "__main__":
    unittest.main()
