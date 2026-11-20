#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按钮接线测试：真的 invoke 界面上每一个按钮，捕获 Tk 回调异常。

覆盖「按钮点了没反应」这类问题（异常被 Tk 的 report_callback_exception 吞掉）。
"""
import os
import sys
import tempfile
import types
import tkinter as tk

TOOL_DIR = r"C:\Z-yuanqilei\AI-workers\workers\DNF\S5A3-运维工具-交接\S5A3-运维工具"
sys.path.insert(0, TOOL_DIR)
import s5a3_deploy_tool as T  # noqa: E402

# 测试不许覆盖真实配置：s5a3_config.json 里是线上地址和明文 root 密码。
# 点按钮会走 _read_form → save_config，以前会把真配置写成测试用的 10.0.0.1 / pw。
T.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "s5a3_ui_test_config.json")
T.CONFIG_FALLBACK = T.CONFIG_PATH


def all_buttons(widget, acc=None):
    acc = [] if acc is None else acc
    for child in widget.winfo_children():
        if isinstance(child, tk.Button):
            acc.append(child)
        all_buttons(child, acc)
    return acc


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    fake_wb = types.ModuleType("webbrowser")
    fake_wb.open = lambda *a, **k: None
    sys.modules["webbrowser"] = fake_wb
    T.filedialog.askopenfilename = lambda *a, **k: ""
    T.filedialog.askdirectory = lambda *a, **k: ""
    T.messagebox.askyesno = lambda *a, **k: True
    T.messagebox.showinfo = lambda *a, **k: None
    T.messagebox.showwarning = lambda *a, **k: None
    T.subprocess.Popen = lambda *a, **k: None

    root = tk.Tk()
    app = T.App(root)
    app.e_host.delete(0, "end")
    app.e_host.insert(0, "10.0.0.1")
    app.e_pwd.delete(0, "end")
    app.e_pwd.insert(0, "pw")

    app._ask_danger = lambda *a, **k: False      # 危险确认一律取消，避免阻塞

    def boom(cfg):
        raise RuntimeError("stub-ssh")
    app._ssh = boom

    errors = []
    root.report_callback_exception = (
        lambda exc, val, tb: errors.append(f"{exc.__name__}: {val}"))

    btns = all_buttons(root)
    print(f"控件树内按钮总数：{len(btns)}")

    for b in btns:
        for x in btns:                 # 上一轮任务会 disable 全部按钮，先恢复
            try:
                x.configure(state="normal")
            except Exception:
                pass
        app.busy = False
        try:
            b.invoke()
        except Exception as e:
            errors.append(f"invoke: {type(e).__name__}: {e}")
        root.update_idletasks()
        root.update()
        app.busy = False
        app._active_btn = None

    if errors:
        print("发现回调异常：")
        for e in sorted(set(errors)):
            print(f"   - {e}  ×{errors.count(e)}")
        print("结论：FAIL")
        root.destroy()
        sys.exit(1)
    print("所有按钮回调无异常")

    # 证明包装器真的把事件交给了业务方法（不依赖后台线程的时序）
    calls = []
    app._start = lambda *a, **k: calls.append(1)
    app._show_page(0)
    root.update_idletasks()
    app._active_btn = None
    app.busy = False
    for x in all_buttons(root):
        try:
            x.configure(state="normal")
        except Exception:
            pass
    app.buttons[0].invoke()
    root.update_idletasks()
    print(f"点击首个业务按钮后，_start 被调用次数：{len(calls)}")
    ok = len(calls) == 1
    print("结论：PASS" if ok else "结论：FAIL（cmd 未被调用）")
    root.destroy()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
