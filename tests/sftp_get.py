#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从服务器 SFTP 拉文件到本地（只读）。用法： python sftp_get.py <远端> <本地> [...]"""
import os
import sys

import paramiko

HOST = os.environ.get("S5A3_HOST", "8.136.178.56")
PW = os.environ["S5A3_PW"]
pairs = sys.argv[1:]
if not pairs or len(pairs) % 2:
    sys.exit("用法: sftp_get.py <远端> <本地> [<远端> <本地> ...]")

t = paramiko.Transport((HOST, 22))
t.connect(username="root", password=PW)
s = paramiko.SFTPClient.from_transport(t)
try:
    for i in range(0, len(pairs), 2):
        remote, local = pairs[i], pairs[i + 1]
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        s.get(remote, local)
        print(f"{remote} -> {local} ({os.path.getsize(local)} B)")
finally:
    s.close()
    t.close()
