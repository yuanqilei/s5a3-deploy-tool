#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传登录器到网关 release_dir 并校验（SFTP，凭据走 S5A3_PW）。"""
import hashlib
import os
import sys

import paramiko

HOST = os.environ.get("S5A3_HOST", "8.136.178.56")
PW = os.environ["S5A3_PW"]
LOCAL = sys.argv[1]
REMOTE = sys.argv[2] if len(sys.argv) > 2 else "/opt/purplemagic/data/releases/PurpleMagic.exe"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


digest = sha256_file(LOCAL)
print(f"local {LOCAL} {os.path.getsize(LOCAL)} B sha256={digest}")

t = paramiko.Transport((HOST, 22))
t.connect(username="root", password=PW)
s = paramiko.SFTPClient.from_transport(t)
try:
    tmp = REMOTE + ".uploading"
    s.put(LOCAL, tmp)
    print("uploaded ->", tmp)
    # 用 exec 通道依次执行
    for cmd in (f"chown dnf90gm:dnf90gm {tmp}",
                f"chmod 644 {tmp}",
                f"mv -f {tmp} {REMOTE}",
                f"ls -l {REMOTE}",
                f"sha256sum {REMOTE}"):
        ch = t.open_session()
        ch.exec_command(cmd)
        out = ch.makefile("r", -1).read()
        ch.recv_exit_status()
        ch.close()
        print(f"$ {cmd}\n{out.strip()}")
finally:
    s.close()
    t.close()
