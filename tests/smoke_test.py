#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S5A3 运维工具 —— 无服务器冒烟测试

用假 SSHClient 替换真连接，把每个动作生成的 shell 脚本 dump 到本目录，
用于人工/程序化检查脚本语法；同时验证 Python 侧逻辑能跑通（不触网、不改服务器）。
"""
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import tkinter as tk

TOOL_DIR = r"C:\Z-yuanqilei\AI-workers\workers\DNF\S5A3-运维工具-交接\S5A3-运维工具"
sys.path.insert(0, TOOL_DIR)

import s5a3_deploy_tool as T  # noqa: E402

# 测试不许覆盖真实配置：s5a3_config.json 里是线上地址和明文 root 密码。
# 每个动作都会 _read_form → save_config，不重定向就会把真配置写成测试假值。
T.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "s5a3_smoke_config.json")
T.CONFIG_FALLBACK = T.CONFIG_PATH

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
OUT = os.path.join(HERE, "dumped_scripts")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def make_fixtures():
    os.makedirs(FIX, exist_ok=True)
    pvf = os.path.join(FIX, "Script.pvf")
    with open(pvf, "wb") as f:
        f.write(b"#PVF_File\n" + b"\x00" * 4096)
    sql = os.path.join(FIX, "dnf_local.sql.gz")
    with open(sql, "wb") as f:
        f.write(b"\x1f\x8b\x08\x00" + b"\x00" * 2048)
    # 源码包：含 runtime/docker/compose.json
    src = os.path.join(FIX, "srctree", "servers5a3")
    os.makedirs(os.path.join(src, "runtime", "docker"), exist_ok=True)
    with open(os.path.join(src, "runtime", "docker", "compose.json"), "w") as f:
        f.write("{}\n")
    with open(os.path.join(src, "s5_mem_guard.go"), "w") as f:
        f.write("package main\n")
    tgz = os.path.join(FIX, "src.tar.gz")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(src, arcname="servers5a3")
    return pvf, sql, tgz


class FakeCli:
    """记录所有命令，按关键字返回可信的假结果。"""

    SHA = "0" * 64
    # 让 verify 走「通过」路径：频道表用定版 sha，compose 发布 120 个游戏端口
    # 直接引用工具里的定版常量，换频道表基线时这里不用再手改
    CHANNEL_SHA = T.CHANNEL_SHA_120

    def __init__(self):
        self.log = []
        self.puts = []
        self.last_put = None

    def _compose(self):
        ports = [7001, 17002] + list(range(10000, 10120))
        return json.dumps({"services": {"server": {"ports": [
            {"target": p, "published": str(p)} for p in ports]}}})

    def run(self, command, on_line=None, timeout=3600, collect=False):
        self.log.append(("RUN", command))
        if on_line:
            for line in ("fake line ok",):
                on_line(line)
        if collect:
            if "mariadb-dump" in command:
                return 0, "/opt/servers5a3/backups/db/dnf_local.FAKE.sql.gz"
            if "install_gm.sh" in command:
                return 0, "S5GM_RESULT=ok"
            return 0, "fake-collected-ok"
        return 0

    def capture(self, command, timeout=120):
        self.log.append(("CAP", command))
        if "docker inspect" in command:
            if ".State.Health" in command:
                return 0, "healthy"
            return 0, "true"
        if "sha256sum" in command and "awk" in command:
            if "channel.override.etc" in command:
                return 0, self.CHANNEL_SHA
            if self.last_put:
                return 0, sha(self.last_put)
            return 0, self.SHA
        if "ss -tln" in command:
            ports = [7001, 17002] + list(range(10000, 10120))
            return 0, "".join(f"LISTEN 0 4096 0.0.0.0:{p} 0.0.0.0:*\n" for p in ports)
        if command.strip().startswith("cat "):
            if "server.env" in command:
                return 0, "DNFBRIDGE_PLAYER_ADMIN_SECRET=" + "A" * 40
            if "compose.json" in command:
                return 0, self._compose()
            if "dnfbridge.toml" in command:
                return 0, 'public_addr = "10.0.0.1:7001"\n'
            if "profile.json" in command:
                return 0, json.dumps({"PvfSha256": self.SHA})
            if "service.json" in command or "settings.json" in command:
                return 0, json.dumps({"adminToken": "A" * 40})
            return 0, ""
        if "stat -c %s" in command:
            if self.last_put:
                return 0, str(os.path.getsize(self.last_put))
            return 0, "0"
        if "cp -f" in command and ".bak" in command:
            for tok in command.split("'"):
                if tok.endswith(".bak"):
                    return 0, tok
            return 0, "/opt/servers5a3/backups/pvf/Script.pvf.FAKE.bak"
        return 0, "yes"

    def sftp_put(self, local, remote, cb=None):
        self.puts.append((local, remote))
        self.last_put = local
        if cb:
            cb(1, 1)

    def sftp_get(self, remote, local, cb=None):
        self.log.append(("GET", f"{remote} -> {local}"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(b"backup" * 200)
        if cb:
            cb(1600, 1600)

    def close(self):
        pass


def dump(name, cli):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        for kind, cmd in cli.log:
            f.write(f"### {kind} ###\n{cmd}\n\n")
        for l, r in cli.puts:
            f.write(f"### PUT ### {l} -> {r}\n")
    return path


def run_case(root, name, setup, action, verbose=False):
    app = T.App(root)
    pvf, sql, tgz = make_fixtures()
    app.e_host.delete(0, "end"); app.e_host.insert(0, "10.0.0.1")
    app.e_pwd.delete(0, "end"); app.e_pwd.insert(0, "pw")
    app.e_path.delete(0, "end"); app.e_path.insert(0, "/opt/servers5a3")
    app.e_patch.delete(0, "end"); app.e_patch.insert(0, "/opt/s5a3-patches")
    app.e_backup.delete(0, "end"); app.e_backup.insert(0, os.path.join(HERE, "local_backups"))
    setup(app, pvf, sql, tgz)
    cfg = app._read_form()
    fake = FakeCli()
    app._ssh = lambda c: fake
    try:
        action(app, cfg)
        print(f"[ok] {name}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    finally:
        p = dump(name, fake)
        print(f"     scripts -> {p}")
        if verbose:
            while True:
                try:
                    page, msg, tag = app.log_queue.get_nowait()
                except Exception:
                    break
                print(f"     [p{page} {tag or '-':4}] {msg}")
        root.update()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()

    def s_git(app, pvf, sql, tgz):
        app.e_pvf_deploy.delete(0, "end"); app.e_pvf_deploy.insert(0, pvf)
        app.e_pvf_replace.delete(0, "end"); app.e_pvf_replace.insert(0, pvf)

    def set_db(app, pvf, sql, tgz):
        s_git(app, pvf, sql, tgz)
        app._db_pending = sql

    run_case(root, "01_test", s_git, lambda a, c: a.do_test(c))
    run_case(root, "02_deploy_git", s_git, lambda a, c: a.do_deploy(c))
    run_case(root, "04_update_git", s_git, lambda a, c: a.do_update(c))
    run_case(root, "05_pvf", s_git, lambda a, c: a.do_pvf(c))
    run_case(root, "06_db_replace", set_db, lambda a, c: a._db_run(c, os.path.join(HERE, "fixtures", "dnf_local.sql.gz"), True, "替换数据库"))
    run_case(root, "07_status", s_git, lambda a, c: a.do_status(c))
    run_case(root, "08_verify", s_git, lambda a, c: a.do_verify(c), verbose=True)
    run_case(root, "10_db_backup", s_git, lambda a, c: a._db_backup(c))

    run_case(root, "09_gm_status", s_git, lambda a, c: a.do_gm_status(c))
    root.destroy()


if __name__ == "__main__":
    main()

