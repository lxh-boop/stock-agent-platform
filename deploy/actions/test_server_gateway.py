"""Offline tests only: no server, production DB, SSH key or network is used."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gateway", Path(__file__).with_name("server_gateway.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def archive(path, files, link=None):
    with tarfile.open(path, "w:gz") as out:
        for name, data in files:
            body = data.encode()
            item = tarfile.TarInfo(name)
            item.size = len(body)
            out.addfile(item, io.BytesIO(body))
        if link:
            item = tarfile.TarInfo(link)
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            out.addfile(item)


class GatewayTests(unittest.TestCase):
    def test_allow_check(self):
        self.assertEqual(g.parse_command("check"), ("check",))

    def test_allow_diagnose(self):
        self.assertEqual(g.parse_command("diagnose"), ("diagnose",))

    def test_allow_deploy_pinned_sha_and_digest(self):
        self.assertEqual(g.parse_command("deploy " + "a" * 40 + " " + "b" * 64)[0], "deploy")

    def test_reject_shell_and_scp(self):
        for value in ["", "bash", "check; id", "check $(id)", "scp -t /tmp/a", "deploy main digest", "deploy " + "a" * 39 + " " + "b" * 64]:
            with self.subTest(value=value), self.assertRaises(g.Refused):
                g.parse_command(value)

    def test_protect_runtime_and_secrets(self):
        for value in ["data/history.csv", "models/best.pth", "deploy/server.env", "runtime_data/users/alice/raw.csv", "local_app_config.json", ".env", "deploy/migration/db.dump", "a/private.key"]:
            self.assertTrue(g.protected(value), value)
        self.assertFalse(g.protected("deploy/server.env.example"))
        self.assertFalse(g.protected("application/service.py"))

    def unpack_sample(self, entries, link=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        package = root / "code.tar.gz"
        required = [(name, "source") for name in ["docker-compose.prod.yml", "Dockerfile.compose", "requirements-compose-api.txt"]]
        archive(package, required + entries, link)
        target = root / "source"
        target.mkdir()
        g.unpack(package, target)
        return target

    def test_extract_source_skip_host_data(self):
        root = self.unpack_sample([("data/raw.csv", "private"), ("deploy/server.env", "secret"), ("app.py", "print('ok')")])
        self.assertTrue((root / "app.py").is_file())
        self.assertFalse((root / "data/raw.csv").exists())
        self.assertFalse((root / "deploy/server.env").exists())

    def test_reject_traversal(self):
        with self.assertRaises(g.Refused):
            self.unpack_sample([("../outside", "bad")])

    def test_reject_absolute_path(self):
        with self.assertRaises(g.Refused):
            self.unpack_sample([("/tmp/outside", "bad")])

    def test_reject_links(self):
        with self.assertRaises(g.Refused):
            self.unpack_sample([], link="linked_secret")

    def test_reject_duplicate_path(self):
        with self.assertRaises(g.Refused):
            self.unpack_sample([("a.py", "1"), ("a.py", "2")])

    def test_reject_noncanonical_path(self):
        with self.assertRaises(g.Refused):
            self.unpack_sample([("a//b.py", "bad")])

    def test_reject_oversized_expansion(self):
        with patch.object(g, "MAX_EXPANDED", 1), self.assertRaises(g.Refused):
            self.unpack_sample([])

    def test_compose_uses_existing_env_and_fixed_project(self):
        command = g.docker_compose(Path("/tmp/stage"))
        self.assertEqual(command[command.index("-p") + 1], "stock_prod")
        self.assertEqual(command[-1], str(g.APP / "deploy/server.env"))

    def test_raw_command_output_not_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = g.Ops(Path(tmp))
            display = io.StringIO()
            result = subprocess.CompletedProcess([], 0, stdout=b"API_KEY=never-public")
            with patch.object(g.subprocess, "run", return_value=result), contextlib.redirect_stdout(display):
                ops.call("diagnostic", ["fake"], capture=True)
            self.assertNotIn("never-public", display.getvalue())
            self.assertIn("never-public", ops.log.read_text())

    def test_failed_command_not_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = g.Ops(Path(tmp))
            display = io.StringIO()
            result = subprocess.CompletedProcess([], 1, stdout=b"password=never-public")
            with patch.object(g.subprocess, "run", return_value=result), contextlib.redirect_stdout(display), self.assertRaises(g.Refused):
                ops.call("failed_test", ["fake"], capture=True)
            self.assertNotIn("never-public", display.getvalue())

    def test_busy_tasks_block_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = g.Ops(Path(tmp))
            with patch.object(ops, "call", return_value="2\n"), self.assertRaisesRegex(g.Refused, "active_tasks"):
                ops.idle()

    def test_empty_active_list_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = g.Ops(Path(tmp))
            with patch.object(ops, "call", return_value="0\n"):
                ops.idle()

    def test_checksum_mismatch_before_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = g.Ops(Path(tmp))
            fake_stdin = type("Input", (), {"buffer": io.BytesIO(b"wrong data")})()
            with patch.object(g.sys, "stdin", fake_stdin), self.assertRaisesRegex(g.Refused, "checksum"):
                ops.deploy("a" * 40, "b" * 64)


if __name__ == "__main__":
    unittest.main()
