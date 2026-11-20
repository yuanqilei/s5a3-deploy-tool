"""Deployment runtime contracts without SSH, Docker, credentials or a GUI."""
import ast
import json
import os
from pathlib import Path
import posixpath
import shlex
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "src/s5a3_deploy_tool.py").read_text(encoding="utf-8-sig"))
names = {"_check_runtime", "_prepare_runtime", "_build_and_up", "_deploy_flow"}
methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
namespace = {"q": shlex.quote, "posixpath": posixpath, "COMPOSE": "runtime/docker/compose.json",
             "DB_C": "servers5a3-db-1", "SERVER_C": "servers5a3-server-1"}
exec(compile(ast.Module(body=methods, type_ignores=[]), "deployment", "exec"), namespace)
RUNTIME = {"runtime/docker/compose.json", "runtime/docker/server.env", "runtime/docker/database.env"}


class SSH:
    def __init__(self, files, failure=False):
        self.files, self.failure = set(files), failure
        self.commands = []
        self.closed = False

    def capture(self, command):
        if command.startswith("head -c"):
            return 0, "stage=build_image\nstatus=failed\ncode=1\n"
        paths = [shlex.split(part)[2] for part in command.split(" || ")]
        return (0 if any(p.removeprefix("/fixture/") in self.files for p in paths) else 1), ""

    def run(self, command, callback, timeout):
        self.commands.append(command)
        if self.failure: return 1
        if "manage.py prepare" in command:
            self.files.update(RUNTIME)
            self.files.discard("runtime/docker/.s5a3-prepare-incomplete")
        return 0

    def close(self): self.closed = True


class Tool:
    _check_runtime = namespace["_check_runtime"]
    _prepare_runtime = namespace["_prepare_runtime"]
    _build_and_up = namespace["_build_and_up"]
    _deploy_flow = namespace["_deploy_flow"]
    def __init__(self, cli): self.cli, self.actions = cli, []
    def _log(self, *args): pass
    def _line_cb(self, line): pass
    def _ssh(self, cfg): return self.cli
    def _source_mode(self, cfg): return "git"
    def _preflight(self, cli, cfg): self.actions.append("preflight")
    def _fetch_sources(self, cli, cfg): self.actions.append("fetch")
    def _restore_patches(self, cli, cfg): self.actions.append("patches")
    def _ensure_pvf(self, cli, cfg): self.actions.append("pvf")
    def _wait_healthy(self, cli, cfg): self.actions.append("health")


class RuntimePreparationTests(unittest.TestCase):
    cfg = {"path": "/fixture"}

    def test_fresh_git_checkout_prepares_missing_runtime(self):
        cli = SSH({"deploy/docker/Dockerfile"})
        Tool(cli)._check_runtime(cli, self.cfg, initialize=True)
        self.assertTrue(RUNTIME.issubset(cli.files))
        self.assertEqual(len(cli.commands), 1)
        self.assertNotIn(" up -d ", cli.commands[0])
        self.assertNotIn("rm -rf", cli.commands[0])
        self.assertLess(cli.commands[0].index("docker volume ls"), cli.commands[0].index("manage.py build"))

    def test_existing_runtime_accepts_tracked_dockerfile_without_regenerating(self):
        for dockerfile in ("deploy/docker/Dockerfile", "runtime/docker/Dockerfile.deploy"):
            cli = SSH(RUNTIME | {dockerfile})
            Tool(cli)._check_runtime(cli, self.cfg, initialize=True)
            self.assertEqual(cli.commands, [])

    def test_partial_runtime_and_update_mode_never_regenerate(self):
        for files, initialize in [(RUNTIME - {"runtime/docker/database.env"}, True), (set(), False)]:
            cli = SSH(files | {"deploy/docker/Dockerfile"})
            with self.assertRaises(RuntimeError): Tool(cli)._check_runtime(cli, self.cfg, initialize)
            self.assertEqual(cli.commands, [])

    def test_interrupted_initialization_is_not_silently_used_in_update_mode(self):
        cli = SSH(RUNTIME | {"deploy/docker/Dockerfile", "runtime/docker/.s5a3-prepare-incomplete"})
        with self.assertRaisesRegex(RuntimeError, "初始化未完成.*构建镜像"):
            Tool(cli)._check_runtime(cli, self.cfg, initialize=False)
        self.assertEqual(cli.commands, [])

    def test_empty_legacy_marker_allows_guarded_retry_without_configuration(self):
        cli = SSH({"deploy/docker/Dockerfile", "runtime/docker/.s5a3-prepare-incomplete"})
        Tool(cli)._check_runtime(cli, self.cfg, initialize=True)
        self.assertTrue(RUNTIME.issubset(cli.files))
        self.assertNotIn("runtime/docker/.s5a3-prepare-incomplete", cli.files)
        self.assertEqual(len(cli.commands), 1)

    def test_retry_failure_keeps_marker_and_never_starts_services(self):
        cli = SSH({"deploy/docker/Dockerfile", "runtime/docker/.s5a3-prepare-incomplete"}, failure=True)
        with self.assertRaisesRegex(RuntimeError, "具体阶段和原始错误"):
            Tool(cli)._check_runtime(cli, self.cfg, initialize=True)
        self.assertIn("runtime/docker/.s5a3-prepare-incomplete", cli.files)
        self.assertFalse(any(" up -d " in command for command in cli.commands))

    def test_resume_validates_existing_credentials_and_backs_up_without_changing_them(self):
        cli = SSH(set())
        Tool(cli)._prepare_runtime(cli, self.cfg)
        code = cli.commands[0].split("python3 - <<'STATE'\n", 1)[1].split("\nSTATE\n", 1)[0]
        compile(code, "resume-validation", "exec")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime/docker"; root.mkdir(parents=True)
            config = {"root_password": "a" * 48, "database_password": "b" * 48, "admin_token": "c" * 64, "host_ip": "192.168.1.2"}
            instance = root / "instance.json"; instance.write_text(json.dumps(config))
            env = root / "database.env"
            env.write_text("MARIADB_ROOT_PASSWORD=" + config["root_password"] + "\nMARIADB_PASSWORD=" + config["database_password"] + "\n")
            marker = root / ".s5a3-prepare-incomplete"; marker.write_text("")
            before = instance.read_bytes(), env.read_bytes()
            previous = Path.cwd()
            try:
                os.chdir(temp); exec(code, {})
                self.assertEqual((instance.read_bytes(), env.read_bytes()), before)
                backups = list((root / "prepare-backups").iterdir())
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / "instance.json").read_bytes(), before[0])
                env.write_text("MARIADB_ROOT_PASSWORD=inconsistent\n")
                with self.assertRaisesRegex(SystemExit, "口令不一致"): exec(code, {})
                self.assertEqual(instance.read_bytes(), before[0])
                marker.unlink()
                with self.assertRaisesRegex(SystemExit, "无中断标记"): exec(code, {})
                instance.unlink()
                with self.assertRaisesRegex(SystemExit, "缺少 instance.json"): exec(code, {})
            finally: os.chdir(previous)

    def test_prepare_failure_prevents_container_start(self):
        cli = SSH({"deploy/docker/Dockerfile"}, failure=True)
        tool = Tool(cli)
        with self.assertRaises(RuntimeError): tool._deploy_flow(self.cfg, False, "fixture")
        self.assertEqual(tool.actions, ["preflight", "fetch", "patches", "pvf"])
        self.assertFalse(any(" up -d " in command for command in cli.commands))
        self.assertTrue(cli.closed)

    def test_existing_deployment_build_uses_dockerfile_fallback(self):
        cli = SSH(set())
        Tool(cli)._build_and_up(cli, self.cfg, False)
        script = cli.commands[0]
        self.assertIn("DOCKERFILE=deploy/docker/Dockerfile", script)
        self.assertIn('-f "$DOCKERFILE"', script)

    def test_recommended_merge_preserves_identity_and_credentials(self):
        cli = SSH(set())
        Tool(cli)._prepare_runtime(cli, self.cfg)
        code = cli.commands[0].split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        compile(code, "recommended-merge", "exec")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / "runtime/docker/instance.json"
            recipe = base / "deploy/templates/docker-instance.recommended.json"
            path.parent.mkdir(parents=True); recipe.parent.mkdir(parents=True)
            existing = {"root_password": "fixture-root", "database_password": "fixture-db", "admin_token": "fixture-token", "host_ip": "192.168.1.2", "runtime_compatibility": {"existing": "on"}}
            path.write_text(json.dumps(existing), encoding="utf-8")
            recipe.write_text(json.dumps({"buff_group_sync": "on", "runtime_compatibility": {"new": "on"}}), encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(base); exec(code, {})
                merged = json.loads(path.read_text())
                for key in ("root_password", "database_password", "admin_token", "host_ip"): self.assertEqual(merged[key], existing[key])
                self.assertEqual(merged["runtime_compatibility"], {"existing": "on", "new": "on"})
                recipe.write_text(json.dumps({"root_password": "forbidden"}))
                before = path.read_bytes()
                with self.assertRaises(SystemExit): exec(code, {})
                self.assertEqual(path.read_bytes(), before)
            finally: os.chdir(previous)


if __name__ == "__main__": unittest.main()
