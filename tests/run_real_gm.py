#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机 GM 网关：部署 / 状态 / 通讯验证，用工具自己的流程。

凭据只从环境变量 S5A3_PW 读，不落盘。
用法： python run_real_gm.py deploy|status|verify|all
"""
import io
import os
import sys
import threading
import time
import tkinter as tk

TOOL_DIR = r"C:\Z-yuanqilei\AI-workers\workers\DNF\S5A3-运维工具-交接\S5A3-运维工具"
sys.path.insert(0, TOOL_DIR)
import s5a3_deploy_tool as T  # noqa: E402

HOST = os.environ.get("S5A3_HOST", "8.136.178.56")
PW = os.environ["S5A3_PW"]
GM_DIR = os.environ.get("S5A3_GM_DIR") or r"C:\DNF\客户端\DNFLauncher\gm\deploy"
# 只用于通讯验证里的「服务端 ↔ 客户端」对账；不给就不填
CLIENT_DIR = os.environ.get("S5A3_CLIENT", "")
ACTION = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "real_gm_%s_%s.log" % (ACTION, time.strftime("%Y%m%d-%H%M%S")))
TAG = {"ok": "OK  ", "err": "FAIL", "gold": "STEP", None: "    "}


def run(app, label, fn, cfg, fh, timeout=5400):
    print(f"\n##### {label} #####", flush=True)
    state = {"done": False, "err": None}
    t0 = time.time()

    def worker():
        try:
            fn(cfg)
        except Exception as e:
            state["err"] = f"{type(e).__name__}: {e}"
        finally:
            state["done"] = True

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    while not state["done"]:
        drained = False
        try:
            while True:
                page, msg, tag = app.log_queue.get_nowait()
                line = f"[{time.strftime('%H:%M:%S')}] {TAG.get(tag, '    ')} {msg}"
                print(line, flush=True)
                fh.write(line + "\n")
                fh.flush()
                drained = True
        except Exception:
            pass
        if not drained:
            time.sleep(0.25)
        if time.time() - t0 > timeout:
            print("!! 超时", flush=True)
            break
    for _ in range(40):
        try:
            page, msg, tag = app.log_queue.get_nowait()
            line = f"[{time.strftime('%H:%M:%S')}] {TAG.get(tag, '    ')} {msg}"
            print(line, flush=True)
            fh.write(line + "\n")
        except Exception:
            break
    fh.flush()
    dur = int(time.time() - t0)
    print(f"===== {label}：{'失败 ' + state['err'] if state['err'] else '成功'} "
          f"用时 {dur // 60} 分 {dur % 60} 秒 =====", flush=True)
    return state["err"] is None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = tk.Tk()
    root.withdraw()
    app = T.App(root)

    def setv(e, v):
        e.delete(0, "end")
        e.insert(0, v)

    setv(app.e_host, HOST)
    setv(app.e_pwd, PW)
    setv(app.e_path, "/opt/servers5a3")
    setv(app.e_advertise, HOST)
    setv(app.e_gm, GM_DIR)
    if CLIENT_DIR:
        setv(app.e_client, CLIENT_DIR)
    cfg = app._read_form()
    print(f"host={cfg['host']} gm_assets={cfg.get('gm_assets')} action={ACTION}", flush=True)

    fh = io.open(OUT, "w", encoding="utf-8", newline="\n")
    results = {}
    steps = [("deploy", "部署 GM 网关", app.do_gm_deploy),
             ("status", "GM 状态", app.do_gm_status),
             ("verify", "通讯验证", app.do_verify)]
    if ACTION != "all":
        steps = [s for s in steps if s[0] == ACTION]
    for key, label, fn in steps:
        results[key] = run(app, label, fn, cfg, fh)
        if key == "deploy" and not results[key]:
            print("!! 部署失败，停止后续步骤", flush=True)
            break

    print("\n===== 汇总 =====", flush=True)
    for k, v in results.items():
        print(f"  {k}: {'OK' if v else 'FAIL'}", flush=True)
    print("完整日志:", OUT, flush=True)
    fh.close()
    root.destroy()


if __name__ == "__main__":
    main()
