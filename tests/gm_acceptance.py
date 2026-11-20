#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GM 网关只读验收（交接文档第四节清单）。

凭据只从环境变量 S5A3_PW 读。只读：不启停服务、不改配置、不打印 token 明文。
用法： python gm_acceptance.py [host]
"""
import json
import os
import sys

import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("S5A3_HOST", "8.136.178.56")
PW = os.environ["S5A3_PW"]

SCRIPT = r"""
echo "### units"
for u in purplemagic-gateway purplemagic-gm nginx; do
  printf 'UNIT %-20s %s\n' "$u" "$(systemctl is-active $u 2>/dev/null || echo missing)"
done
echo "### ports"
ss -tln | awk 'NR>1{print $4}' | grep -E ':(80|443|8443|18080|18444|18081)$' | sort -u | sed 's/^/PORT /'
echo "### console"
printf 'CONSOLE_LOCAL %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:18080/console/ || echo err)"
printf 'CONSOLE_80    %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1/console/ || echo err)"
echo "### api"
printf 'STATUS_80     %s\n' "$(curl -s -m 8 http://127.0.0.1/api/status | head -c 400)"
printf 'STATUS_8443   %s\n' "$(curl -sk -m 8 https://127.0.0.1:8443/api/status | head -c 400)"
printf 'BOOTSTRAP     %s\n' "$(curl -sk -o /dev/null -w '%{http_code}' -m 8 https://127.0.0.1:8443/launcher/v2/bootstrap.json || echo err)"
echo "### token"
if [ -s /opt/purplemagic/data/admin-token.txt ]; then
  printf 'TOKEN_LEN %s\n' "$(tr -d '\r\n' < /opt/purplemagic/data/admin-token.txt | wc -c)"
  printf 'TOKEN_CHARSET %s\n' "$(tr -d '\r\n' < /opt/purplemagic/data/admin-token.txt | grep -qE '^[A-Za-z0-9._-]+$' && echo ok || echo odd)"
else
  echo 'TOKEN_MISSING'
fi
echo "### index"
ls -l /opt/purplemagic/data/indexes 2>/dev/null | sed 's/^/IDX /'
echo "### profile"
/usr/bin/python3 - <<'PY' 2>/dev/null || echo PROFILE_UNREADABLE
import json
for p in ("/opt/purplemagic/data/profile.json", "/opt/purplemagic/profile.json"):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    print("PROFILE_PATH", p)
    for k in ("ApiUrl", "apiUrl", "PvfSha256", "pvfSha256", "GameHost", "BootstrapPort"):
        if k in d:
            v = d[k]
            if isinstance(v, str) and len(v) > 24 and "sha" in k.lower():
                v = v[:16] + "…"
            print("PROFILE", k, v)
    break
PY
echo "### version"
/opt/purplemagic/bin/GMTool -version 2>/dev/null | head -3 | sed 's/^/VER /' || true
"""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, "root", PW, timeout=20, allow_agent=False, look_for_keys=False)
    try:
        _, o, e = c.exec_command(SCRIPT, timeout=180)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
    finally:
        c.close()
    print(out)
    if err.strip():
        print("--- stderr ---")
        print(err[:1500])

    print("=== 判定 ===")
    checks = []
    units = dict(l.split()[1:3] for l in out.splitlines()
                 if l.startswith("UNIT ") and len(l.split()) >= 3)
    for u in ("purplemagic-gateway", "purplemagic-gm", "nginx"):
        checks.append((f"{u} active", units.get(u) == "active", units.get(u, "?")))

    ports = {l.split(":", 1)[1] for l in out.splitlines() if l.startswith("PORT ")}
    for p in ("80", "443", "8443", "18080", "18444"):
        checks.append((f"端口 {p} 监听", p in ports, "有" if p in ports else "无"))

    def val(prefix):
        for l in out.splitlines():
            if l.startswith(prefix):
                return l[len(prefix):].strip()
        return ""

    checks.append(("/console/ 本机 200", val("CONSOLE_LOCAL") == "200", val("CONSOLE_LOCAL")))
    checks.append(("/console/ 经 80 200", val("CONSOLE_80") == "200", val("CONSOLE_80")))
    # 注意两个 /api/status 不是同一个东西：
    #   80   -> GM 面板（{"authenticated":false,"configured":true,...}）
    #   8443 -> 游戏账号 API（{"ok":true,"message":"dnf90 account api ready"}）
    checks.append(("/api/status 经 80 = GM 面板", '"configured"' in val("STATUS_80"),
                   val("STATUS_80")[:80]))
    checks.append(("/api/status 经 8443 = 账号 API ok",
                   '"ok":true' in val("STATUS_8443").replace(" ", ""), val("STATUS_8443")[:80]))
    checks.append(("bootstrap.json 200", val("BOOTSTRAP") == "200", val("BOOTSTRAP")))
    try:
        tlen = int(val("TOKEN_LEN") or 0)
    except ValueError:
        tlen = 0
    checks.append(("admin-token.txt 存在且 ≥32 位", tlen >= 32, f"{tlen} 位"))

    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
