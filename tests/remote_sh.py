#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在服务器上跑一段只读 shell，打印 stdout/stderr。

用法： python remote_sh.py <本地脚本文件>
凭据只从环境变量 S5A3_PW 读。
"""
import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = os.environ.get("S5A3_HOST", "8.136.178.56")
PW = os.environ["S5A3_PW"]
SH = sys.argv[1] if len(sys.argv) > 1 else "-"
TIMEOUT = int(os.environ.get("S5A3_T", "120"))

script = sys.stdin.read() if SH == "-" else open(SH, encoding="utf-8").read()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, 22, "root", PW, timeout=20, allow_agent=False, look_for_keys=False)
try:
    _, o, e = c.exec_command(script, timeout=TIMEOUT)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
finally:
    c.close()
print(out)
if err.strip():
    print("--- stderr ---")
    print(err[:2000])
