#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S5A3 服务端一键部署工具

功能：检测连接 / 一键部署 / 更新服务端 / 替换 PVF / 导入数据库 / 查看状态
依赖：paramiko、Pillow

2026-11-17 修订（运维范畴，不含 GM）：
  1. 部署/更新前从补丁目录恢复三个私有补丁；源码树里找不到 s5_mem_guard.go 直接拒绝构建
  2. 源码来源支持「远程仓库」与「本机源码包（.tar.gz/.tgz/.zip）」二选一
  3. PVF 拆成「部署 PVF」（空服务器初始化上传）与「替换 PVF」两个字段
  4. 备份统一落服务器 <部署路径>/backups/，并自动下载到本机备份目录
  5. 上传一律先传临时文件，校验大小 + SHA256 通过后才原子替换，失败保留旧文件
  6. 检测连接增加内存 / docker compose v2 检查；导库前自动整库备份并下载
"""

import base64
import hashlib
import json
import os
import posixpath
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import paramiko
except ImportError:
    raise SystemExit("缺少 paramiko：pip install paramiko")

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None

# ---------------- 日志文本清洗 ----------------
# docker build / compose 的进度输出带 ANSI 控制序列、还用回车原地覆盖。
# 原样塞进日志框会把界面拖死（2026-11-18 实机复现：单核打满、Windows 判定「未响应」）。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"            # CSI（颜色、光标、清屏）
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC（标题等）
    r"|\x1b[@-Z\\-_]"                       # 两字节转义
)


def strip_ansi(text):
    """去掉 ANSI 控制序列和残留控制字符，只留下可读文本。"""
    if not text:
        return ""
    text = _ANSI_RE.sub("", text)
    return "".join(ch for ch in text if ch >= " " or ch == "\t").rstrip()

# ---------------- 路径 ----------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR

CONFIG_PATH = os.path.join(APP_DIR, "s5a3_config.json")
# exe 目录不可写时（只读介质/受控目录）退到这里
CONFIG_FALLBACK = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "S5A3", "s5a3_config.json")
BG_FILE = os.path.join(RES_DIR, "S5A3BG.jpg")

# ---------------- 服务端固定名 ----------------
SERVER_C = "servers5a3-server-1"
DB_C = "servers5a3-db-1"
DB_NAME = "dnf_local"
COMPOSE = "runtime/docker/compose.json"
PVF_REL = "runtime/docker/data/Script.pvf"
DBENV_REL = "runtime/docker/database.env"
AWK_FIRST = "awk '{print $1}'"
# 容器健康状态模板：给 docker 的 Go 模板必须是双花括号；这里用普通字符串，
# 外层 f-string 只做整体替换，不会再被当成自己的花括号。
INSPECT_HEALTH = ("'{{if .State.Health}}{{.State.Health.Status}}"
                  "{{else}}{{.State.Status}}{{end}}'")

DEFAULTS = {
    "host": "",
    "user": "root",
    "password": "",
    "repo": "git@github-qingqing:yuanqilei/qingqing.git",
    "local_pkg": "",
    "path": "/opt/servers5a3",
    "patch_dir": "/opt/s5a3-patches",
    "pvf_deploy": "",
    "pvf_replace": "",
    "backup_dir": os.path.join(APP_DIR, "backups"),
    # 网关 / 登录器 / 通讯验证
    "pm_src": "/opt/purplemagic-src",
    "pm_repo": "git@github-xianren:yuanqilei/xianren.git",
    "pm_data": "/opt/purplemagic/data2",
    "launcher_exe": "",
    "client_dir": "",
    "advertise_ip": "",
    "channel_table": "",
    "release_dir": "",
    "bundle_pkg": "",
    "update_pkg": "",
    "pack_src": "",
    "pack_version": "",
}

# PurpleMagic 部署位置（install_gm.sh 里写死的，改不了）
GM_BASE = "/opt/purplemagic"
# DNF90GM（独立 GM 后台，2026-11-18 起替代 PurpleMagic GM）
GM90_DIR = "/opt/dnf90gm"
GM90_BIN = GM90_DIR + "/DNF90GM"
GM90_CONF = GM90_DIR + "/runtime/gm.json"
GM90_PORT = 18112
# 随包定版外部频道表（120 条）的 SHA256
# 2026-11-18 起换成上游统一基线：超时空 261/262/263 → 244/245/246
# 旧值 e04a8075…（261-263）不再有效，改表后续沿用本条
CHANNEL_SHA_120 = "5630b099ef98e322806c35cec25be7c7aae699ebfbc8f16e60f1a70d12bfe7d7"
CHANNEL_COUNT_120 = 120
GAME_PORT_LO = 10000
GAME_PORT_HI = 11000

# ---------------- 配色（呼应背景图的深蓝 + 金色）----------------
# 视觉规范（见《S5A3服务端管理工具_UI优化交接文档》第 5 节）
C_PANEL = "#0F1A2B"          # 面板底（实色）
C_PANEL_HI = "#16263D"       # 次级底色（工具栏/按钮）
C_TEXT = "#E8EDF5"           # 主文字
C_SECOND = "#C7D2E0"         # 次要文字
C_DIM = "#9AA8BD"            # 提示文字
C_GOLD = "#F5B942"           # 主操作
C_GOLD_HI = "#FFD166"
C_DANGER = "#E24B4A"         # 危险
C_OK = "#3FB27F"             # 成功
C_INFO = "#4A9EFF"
C_LINE = "#2A3B55"           # 边框
C_ENTRY = "#0B1524"
C_LOG_BG = "#0A1220"
C_ERR = C_DANGER             # 兼容旧调用

F_TITLE = ("Microsoft YaHei UI", 15, "bold")
F_GROUP = ("Microsoft YaHei UI", 10, "bold")
F_LABEL = ("Microsoft YaHei UI", 10)
F_HINT = ("Microsoft YaHei UI", 9)
F_BTN = ("Microsoft YaHei UI", 10)
F_ENTRY = ("Consolas", 10)
F_LOG = ("Consolas", 9)


TOOL_VERSION = "2026.11.19"
TOOL_BUILD = "2026-11-19"


def _dpapi(data, protect):
    """调 Windows DPAPI（当前用户）加/解密；失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(DATA_BLOB)
        if protect:
            crypt32.CryptProtectData.argtypes = [blob_p, wintypes.LPCWSTR, blob_p,
                                                 ctypes.c_void_p, ctypes.c_void_p,
                                                 wintypes.DWORD, blob_p]
            crypt32.CryptProtectData.restype = wintypes.BOOL
        else:
            crypt32.CryptUnprotectData.argtypes = [blob_p, ctypes.c_void_p, blob_p,
                                                   ctypes.c_void_p, ctypes.c_void_p,
                                                   wintypes.DWORD, blob_p]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

        def make(raw):
            buf = ctypes.create_string_buffer(raw, max(len(raw), 1))
            return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        entropy, keep_ent = make(b"S5A3-Tool-Config-v1")
        source, keep_src = make(data)
        out = DATA_BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if not fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1,
                  ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, wintypes.HLOCAL))
    except Exception:
        return None


def protect_password(password):
    """明文 -> DPAPI(base64)。非 Windows 或失败返回空串。"""
    if not password or os.name != "nt":
        return ""
    raw = _dpapi(password.encode("utf-8"), True)
    return base64.b64encode(raw).decode("ascii") if raw else ""


def unprotect_password(token):
    """DPAPI(base64) -> 明文。解不开返回空串（换机器/换用户会这样）。"""
    if not token:
        return ""
    try:
        raw = _dpapi(base64.b64decode(token), False)
        return raw.decode("utf-8") if raw else ""
    except Exception:
        return ""


def load_config():
    for path in (CONFIG_PATH, CONFIG_FALLBACK):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 老版本的单个 pvf 字段 -> 替换 PVF
            if data.get("pvf") and not data.get("pvf_replace"):
                data["pvf_replace"] = data["pvf"]
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in data.items() if k in DEFAULTS})
            # 密码：优先 DPAPI 密文；旧版明文仍可读
            if data.get("password_enc"):
                plain = unprotect_password(data["password_enc"])
                merged["password"] = plain
                if not plain:
                    merged["_password_locked"] = True
            elif data.get("password"):
                merged["password"] = data["password"]
            # backup_dir 曾落在 dist\\backups（PyInstaller 输出目录，会被重建清掉）
            bd = (merged.get("backup_dir") or "").rstrip("\\/")
            if bd.lower().endswith("\\dist\\backups"):
                merged["backup_dir"] = bd[: -len("dist\\backups")].rstrip("\\/") + os.sep + "backups"
            return merged
        except Exception:
            pass
    return dict(DEFAULTS)


def save_config(cfg):
    """写配置：先写 exe 同目录，失败再退到 %LOCALAPPDATA%。返回 (是否成功, 实际路径, 错误)。"""
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    enc = protect_password(data.pop("password", "") or "")
    if enc:
        data["password_enc"] = enc          # 只落密文
    else:
        data["password"] = cfg.get("password", "")   # 非 Windows/加密失败才退回明文
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_err = ""
    for path in (CONFIG_PATH, CONFIG_FALLBACK):
        try:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)          # 原子替换，避免半截文件
            return True, path, ""
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, "", last_err


def q(s):
    """shell 单引号转义。远程路径一律用它包。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def sha256_file(path, on_progress=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(done, total)
    return h.hexdigest()


class SSHClient:
    def __init__(self, host, user, password):
        self.host, self.user, self.password = host, user, password
        self.client = None

    def connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host, port=22, username=self.user, password=self.password,
            timeout=15, look_for_keys=False, allow_agent=False,
        )
        return self

    def run(self, command, on_line=None, timeout=3600, collect=False):
        """流式执行。collect=True 时返回 (rc, 全部输出)。"""
        if not self.client:
            self.connect()
        stdin, stdout, stderr = self.client.exec_command(
            command, timeout=timeout, get_pty=True)
        chan = stdout.channel
        buf = ""
        lines = []
        while True:
            if chan.recv_ready():
                buf += chan.recv(8192).decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if collect:
                        lines.append(line)
                    if on_line:
                        on_line(line)
            elif chan.exit_status_ready() and not chan.recv_ready():
                break
            else:
                time.sleep(0.04)
        if buf:
            line = buf.rstrip("\r")
            if collect:
                lines.append(line)
            if on_line:
                on_line(line)
        rc = chan.recv_exit_status()
        return (rc, "\n".join(lines)) if collect else rc

    def capture(self, command, timeout=120):
        """静默执行，返回 (rc, stdout+stderr)。用于判断类命令。"""
        if not self.client:
            self.connect()
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        # 上限放宽到 200KB：`ss -tln` 在 120 个频道端口时就能超过 8KB，
        # 截断会导致「已发布但未监听」误判。
        return rc, (out + err).strip()[:200000]

    def sftp_put(self, local_path, remote_path, on_progress=None):
        if not self.client:
            self.connect()
        sftp = self.client.open_sftp()
        try:
            sftp.put(local_path, remote_path, callback=on_progress)
        finally:
            sftp.close()

    def sftp_get(self, remote_path, local_path, on_progress=None):
        if not self.client:
            self.connect()
        sftp = self.client.open_sftp()
        try:
            sftp.get(remote_path, local_path, callback=on_progress)
        finally:
            sftp.close()

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


# ---------------- UI 组件 ----------------
class Tooltip:
    """悬停提示：一行示例值 + 一句说明。"""

    def __init__(self, widget, text, delay=420):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", bg=C_PANEL_HI, fg=C_SECOND,
                 font=F_HINT, padx=9, pady=6, highlightthickness=1,
                 highlightbackground=C_LINE).pack()

    def _hide(self, _=None):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


LEVEL_COLOR = {"INFO": "#8FA0B8", "OK": C_OK, "WARN": C_GOLD, "ERR": C_DANGER}
LEVEL_ORDER = ("ALL", "INFO", "OK", "WARN", "ERR")
TAG_TO_LEVEL = {"ok": "OK", "err": "ERR", "warn": "WARN", "gold": "INFO",
                "info": "INFO", None: "INFO"}


class ScrollForm(tk.Frame):
    """可滚动表单容器：内容高于窗格时滚动，避免字段被裁掉；宽度随窗格伸缩。"""

    def __init__(self, master):
        super().__init__(master, bg=C_PANEL)
        self.canvas = tk.Canvas(self, bg=C_PANEL, highlightthickness=0, bd=0)
        self.vs = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                               bg=C_PANEL, troughcolor=C_PANEL, activebackground=C_GOLD,
                               relief="flat", width=9)
        self.canvas.configure(yscrollcommand=self.vs.set)
        self.vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=C_PANEL, padx=2, pady=2)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _bind_wheel(self, _=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _=None):
        self.canvas.unbind_all("<MouseWheel>")

class LogPane(tk.Frame):
    """单页日志：时间戳 + 级别颜色 + 搜索 + 级别筛选 + 自动滚动 + 展开/收起。"""

    MAX_KEEP = 4000

    def __init__(self, master, paned=None):
        super().__init__(master, bg=C_PANEL)
        self.paned = paned
        self.records = []
        self.level = "ALL"
        self.query = ""
        self.autoscroll = tk.BooleanVar(value=True)
        self._saved_sash = None
        self._build()

    def _build(self):
        bar = tk.Frame(self, bg=C_PANEL)
        bar.pack(fill="x", pady=(2, 4))
        tk.Label(bar, text="日志", bg=C_PANEL, fg=C_SECOND, font=F_GROUP).pack(side="left")

        self.btn_fold = tk.Button(bar, text="收起", command=self.toggle_fold, bg=C_PANEL_HI,
                                  fg=C_SECOND, relief="flat", font=F_HINT, padx=8, pady=2,
                                  activebackground=C_GOLD, activeforeground="#1A1204",
                                  cursor="hand2", bd=0)
        self.btn_fold.pack(side="right")
        self.e_search = tk.Entry(bar, width=16, bg=C_ENTRY, fg=C_TEXT,
                                 insertbackground=C_GOLD, relief="flat", font=F_HINT,
                                 highlightthickness=1, highlightbackground=C_LINE,
                                 highlightcolor=C_GOLD)
        self.e_search.pack(side="right", padx=(4, 6), ipady=2)
        self.e_search.bind("<KeyRelease>", lambda e: self.reload())
        tk.Label(bar, text="搜索", bg=C_PANEL, fg=C_DIM, font=F_HINT).pack(side="right")

        self.lv_btn = tk.Menubutton(bar, text="全部 ▾", bg=C_PANEL_HI, fg=C_SECOND,
                                    activebackground=C_GOLD, activeforeground="#1A1204",
                                    relief="flat", font=F_HINT, padx=8, pady=2,
                                    cursor="hand2", bd=0)
        menu = tk.Menu(self.lv_btn, tearoff=False, bg=C_PANEL_HI, fg=C_TEXT,
                       activebackground=C_GOLD, activeforeground="#1A1204", font=F_HINT)
        for lv in LEVEL_ORDER:
            menu.add_command(label=lv, command=lambda v=lv: self.set_level(v))
        self.lv_btn.configure(menu=menu)
        self.lv_btn.pack(side="right", padx=(0, 6))

        tk.Checkbutton(bar, text="自动滚动", variable=self.autoscroll, bg=C_PANEL, fg=C_DIM,
                       activebackground=C_PANEL, activeforeground=C_TEXT,
                       selectcolor=C_ENTRY, font=F_HINT, bd=0,
                       highlightthickness=0).pack(side="right", padx=(0, 8))

        self.btn_clear = tk.Button(bar, text="清空", command=self.clear, bg=C_PANEL_HI,
                                   fg=C_SECOND, relief="flat", font=F_HINT, padx=8, pady=2,
                                   activebackground=C_DANGER, activeforeground="#ffffff",
                                   cursor="hand2", bd=0)
        self.btn_clear.pack(side="right", padx=(0, 6))

        wrap = tk.Frame(self, bg=C_LINE, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, bg=C_LOG_BG, fg=C_TEXT, insertbackground=C_GOLD,
                           relief="flat", font=F_LOG, wrap="word", height=6,
                           padx=8, pady=6, state="disabled")
        sb = tk.Scrollbar(wrap, command=self.txt.yview, bg=C_PANEL, troughcolor=C_LOG_BG,
                          activebackground=C_GOLD, relief="flat", width=10)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        for lv, color in LEVEL_COLOR.items():
            self.txt.tag_configure(lv, foreground=color)

    # ---- 写入 ----
    def append(self, msg, tag=None):
        self.append_many([(msg, tag)])

    def append_many(self, items):
        """批量写入：一次状态切换 + 一次自动滚动，避免逐条插入拖死主线程。"""
        if not items:
            return
        ts = time.strftime("%H:%M:%S")
        at_bottom = self.txt.yview()[1] > 0.995
        chunk = []
        for msg, tag in items:
            level = TAG_TO_LEVEL.get(tag, "INFO")
            self.records.append((ts, level, msg))
            if self._match(level, msg):
                chunk.append((level, self._line(ts, level, msg)))
        if len(self.records) > self.MAX_KEEP:
            del self.records[:len(self.records) - self.MAX_KEEP]
        if chunk:
            self.txt.configure(state="normal")
            for level, text in chunk:
                self.txt.insert("end", text, level)
            self.txt.configure(state="disabled")
            if self.autoscroll.get() and at_bottom:
                self.txt.see("end")

    def _match(self, level, msg):
        if self.level != "ALL" and level != self.level:
            return False
        if self.query and self.query.lower() not in msg.lower():
            return False
        return True

    def _line(self, ts, level, msg):
        return f"{ts}  [{level:<4}]  {msg}\n"

    def _insert(self, ts, level, msg):
        at_bottom = self.txt.yview()[1] > 0.995
        self.txt.configure(state="normal")
        self.txt.insert("end", self._line(ts, level, msg), level)
        self.txt.configure(state="disabled")
        if self.autoscroll.get() and at_bottom:
            self.txt.see("end")

    # ---- 过滤 / 搜索 ----
    def reload(self):
        self.query = self.e_search.get().strip()
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for ts, lv, msg in self.records:
            if self._match(lv, msg):
                self.txt.insert("end", self._line(ts, lv, msg), lv)
        self.txt.configure(state="disabled")
        if self.autoscroll.get():
            self.txt.see("end")

    def set_level(self, lv):
        self.level = lv
        self.lv_btn.configure(text=("全部" if lv == "ALL" else lv) + " ▾")
        self.reload()

    def clear(self):
        self.records.clear()
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # ---- 展开 / 收起 ----
    def toggle_fold(self):
        if self.paned is None:
            return
        h = self.paned.winfo_height()
        if self.btn_fold.cget("text") == "收起":
            self._saved_sash = h if h > 200 else None
            self.paned.sash_place(0, 0, max(h - 74, 40))
            self.btn_fold.configure(text="展开")
        else:
            target = int(h * 0.72) if h > 200 else 300
            self.paned.sash_place(0, 0, target)
            self.btn_fold.configure(text="收起")


class DangerDialog(tk.Toplevel):
    """危险操作二次确认：必须输入 CONFIRM，且显示本次实际操作目标。"""

    def __init__(self, parent, title, lines, target, typed=True):
        super().__init__(parent)
        self.ok = not typed            # 不要求输入时，直接允许确定
        self.typed = typed
        self.title("请确认危险操作")
        self.configure(bg=C_PANEL)
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        wrap = tk.Frame(self, bg=C_PANEL, padx=18, pady=16)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=f"⚠  {title}", bg=C_PANEL, fg=C_DANGER, font=F_TITLE).pack(anchor="w")
        tk.Frame(wrap, bg=C_LINE, height=1).pack(fill="x", pady=(10, 10))
        for ln in lines:
            tk.Label(wrap, text=ln, bg=C_PANEL, fg=C_SECOND, font=F_LABEL,
                     justify="left", wraplength=520).pack(anchor="w", pady=1)

        tk.Label(wrap, text="本次操作目标", bg=C_PANEL, fg=C_DIM,
                 font=F_HINT).pack(anchor="w", pady=(12, 2))
        tgt = tk.Entry(wrap, width=64, bg=C_ENTRY, fg=C_GOLD, relief="flat",
                       font=("Consolas", 9), highlightthickness=1,
                       highlightbackground=C_LINE, readonlybackground=C_ENTRY)
        tgt.insert(0, str(target))
        tgt.configure(state="readonly")
        tgt.pack(anchor="w", ipady=4)

        if typed:
            tk.Label(wrap, text="此操作不可撤销。输入 CONFIRM 继续：", bg=C_PANEL,
                     fg=C_DANGER, font=F_HINT).pack(anchor="w", pady=(12, 2))
            self.e_confirm = tk.Entry(wrap, width=24, bg=C_ENTRY, fg=C_TEXT,
                                      insertbackground=C_GOLD, relief="flat", font=F_ENTRY,
                                      highlightthickness=1, highlightbackground=C_LINE,
                                      highlightcolor=C_DANGER)
            self.e_confirm.pack(anchor="w", ipady=4)
            self.e_confirm.bind("<KeyRelease>", self._check)
            self.e_confirm.focus_set()
        else:
            tk.Label(wrap, text="确认后立即执行，此操作不可撤销。", bg=C_PANEL,
                     fg=C_DANGER, font=F_HINT).pack(anchor="w", pady=(12, 2))

        bar = tk.Frame(wrap, bg=C_PANEL)
        bar.pack(fill="x", pady=(16, 0))
        self.btn_ok = tk.Button(bar, text="确定执行" if not typed else "继续执行",
                                command=self._ok,
                                state="normal" if not typed else "disabled",
                                bg=C_DANGER if not typed else "#3A1A1A",
                                fg="#ffffff" if not typed else "#7A5A5A",
                                relief="flat", font=F_BTN, padx=16, pady=5,
                                cursor="hand2", bd=0,
                                highlightthickness=1, highlightbackground=C_DANGER)
        self.btn_ok.pack(side="right")
        tk.Button(bar, text="取消", command=self._cancel, bg=C_PANEL_HI, fg=C_SECOND,
                  relief="flat", font=F_BTN, padx=16, pady=5, cursor="hand2",
                  activebackground=C_LINE, bd=0).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Return>", lambda e: self._ok() if self.ok else None)
        self.grab_set()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _check(self, _=None):
        if self.e_confirm.get().strip() == "CONFIRM":
            self.ok = True
            self.btn_ok.configure(state="normal", bg=C_DANGER, fg="#ffffff")
        else:
            self.ok = False
            self.btn_ok.configure(state="disabled", bg="#3A1A1A", fg="#7A5A5A")

    def _ok(self):
        if not self.ok:
            return
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.ok = False
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class App:
    _RE_ERR = re.compile(r"错误|失败|error|fatal|denied|traceback", re.I)
    _RE_WARN = re.compile(r"警告|warn", re.I)
    _RE_OK = re.compile(r"\b(ok|healthy|done)\b|完成|就绪|通过")

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self._last_gm_password = ""
        self.log_queue = queue.Queue()
        self.busy = False
        self.bg_photo = None
        self.bg_item = None
        self._resize_job = None
        self.buttons = []
        self.nav_buttons = []
        self.pages = []
        self.logs = []
        self.page_index = 0
        self.log_target = 0
        self.log_sink_page = 0
        self._building_page = 0
        self._active_btn = None
        self._t0 = 0.0
        self._connected = False
        self.ui_queue = queue.Queue()
        self.pw_list = []
        self.form_list = []
        self.bar_list = []
        self._cur_actions = None
        self._sash_user = {}
        self._sash_expect = {}
        self._build_ui()
        self._pump_log()
        self._apply_bg()
        self.root.bind("<Configure>", self._on_resize)

    # ---------------- 背景 ----------------
    def _dark_titlebar(self):
        """Windows 10/11 深色标题栏。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    def _apply_bg(self):
        w = max(self.root.winfo_width(), 1000)
        h = max(self.root.winfo_height(), 760)
        if Image is None:
            self.canvas.configure(bg="#0a0e17")
            return
        try:
            img = Image.open(BG_FILE).convert("RGB")
        except Exception:
            self.canvas.configure(bg="#0a0e17")
            return
        img = ImageOps.fit(img, (w, h), Image.LANCZOS)
        overlay = Image.new("RGBA", (w, h), (6, 10, 18, 130))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        self.bg_photo = ImageTk.PhotoImage(img)
        if self.bg_item is None:
            self.bg_item = self.canvas.create_image(0, 0, anchor="nw", image=self.bg_photo)
        else:
            self.canvas.itemconfig(self.bg_item, image=self.bg_photo)

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(220, self._apply_bg)

    # ---------------- UI ----------------
    def _build_ui(self):
        self.root.title("S5A3 服务端管理  v%s" % TOOL_VERSION)
        w, h = 1060, 820
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 2 - 40, 16)}")
        self.root.minsize(960, 720)
        self.root.configure(bg="#080E18")
        self._dark_titlebar()

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bg="#080E18")
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

        panel = tk.Frame(self.root, bg=C_PANEL, highlightbackground=C_LINE,
                         highlightthickness=1)
        panel.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.95, relheight=0.95)

        # 标题区
        head = tk.Frame(panel, bg=C_PANEL)
        head.pack(fill="x", padx=16, pady=(12, 4))
        left = tk.Frame(head, bg=C_PANEL)
        left.pack(side="left")
        tk.Label(left, text="S5A3 服务端管理", bg=C_PANEL, fg=C_TEXT,
                 font=F_TITLE).pack(anchor="w")
        tk.Label(left, text="部署 · 数据库 · 登录器 · GM", bg=C_PANEL, fg=C_DIM,
                 font=F_HINT).pack(anchor="w")

        right = tk.Frame(head, bg=C_PANEL)
        right.pack(side="right")
        for text, cmd in (("保存配置", self._save_cfg_click), ("重置配置", self._reset_cfg_click)):
            tk.Button(right, text=text, command=cmd, bg=C_PANEL_HI, fg=C_SECOND,
                      relief="flat", font=F_HINT, padx=10, pady=3, cursor="hand2",
                      activebackground=C_LINE, bd=0).pack(side="left", padx=(6, 0))

        tk.Frame(panel, bg=C_LINE, height=1).pack(fill="x", padx=16, pady=(8, 6))

        # 页签
        nav = tk.Frame(panel, bg=C_PANEL)
        nav.pack(fill="x", padx=16, pady=(0, 2))
        for i, text in enumerate(("游戏服务端", "数据库管理", "登录器服务端", "GM工具")):
            b = tk.Button(nav, text=text, command=lambda i=i: self._show_page(i),
                          bg=C_PANEL_HI, fg=C_SECOND, relief="flat", font=F_BTN,
                          padx=14, pady=6, cursor="hand2", bd=0,
                          highlightthickness=0)
            b.pack(side="left", padx=(0, 6))
            self.nav_buttons.append(b)

        # 页面区（每页自带 表单/日志 可拖拽分隔）
        self.page_box = tk.Frame(panel, bg=C_PANEL)
        self.page_box.pack(fill="both", expand=True, padx=16, pady=(6, 2))
        self.page_box.grid_rowconfigure(0, weight=1)
        self.page_box.grid_columnconfigure(0, weight=1)

        self.logs = []
        self.pages = [
            self._page_deploy(self.page_box),
            self._page_db(self.page_box),
            self._page_launcher(self.page_box),
            self._page_gm(self.page_box),
        ]

        # 状态栏：左 连接 / 中 进度 / 右 当前页 + 耗时
        tk.Frame(panel, bg=C_LINE, height=1).pack(fill="x", padx=16, pady=(2, 0))
        bar = tk.Frame(panel, bg=C_PANEL)
        bar.pack(side="bottom", fill="x", padx=16, pady=(4, 8))
        self.st_right = tk.Label(bar, text="游戏服务端", bg=C_PANEL, fg=C_DIM, font=F_HINT)
        self.st_right.pack(side="right")
        self.st_conn = tk.Label(bar, text="● 未连接", bg=C_PANEL, fg=C_DANGER, font=F_HINT)
        self.st_conn.pack(side="left")
        self.st_mid = tk.Label(bar, text="就绪", bg=C_PANEL, fg=C_DIM, font=F_HINT)
        self.st_mid.pack(side="left", expand=True, fill="x", padx=(12, 12))

        self._show_page(0)

    # ---------------- 基础控件 ----------------
    def _mkbtn(self, parent, text, cmd, width=None, primary=False, danger=False):
        if danger:
            bg, fg, border = "#2A1616", "#FF8A89", C_DANGER
            if primary:
                bg, fg = "#3A1A1A", "#FFB3B2"
        elif primary:
            bg, fg, border = C_GOLD, "#1A1204", C_GOLD
        else:
            bg, fg, border = C_PANEL_HI, C_SECOND, C_LINE
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=C_GOLD if not danger else C_DANGER,
                      activeforeground="#1A1204" if not danger else "#ffffff",
                      relief="flat", bd=0, padx=13, pady=5, cursor="hand2",
                      font=F_BTN, highlightthickness=1, highlightbackground=border)
        if width:
            b.configure(width=width)
        return b

    def _add_btn(self, parent, text, cmd, primary=False, danger=False, side="left"):
        label = ("⚠ " + text) if danger else text
        holder = {}
        page = self._building_page

        def run(cmd=cmd):
            b = holder.get("b")
            self.log_target = page
            self._active_btn = (b, b.cget("text")) if b is not None else (None, label)
            cmd()

        b = self._mkbtn(parent, label, run, primary=primary, danger=danger)
        holder["b"] = b
        b.pack(side=side, padx=(0, 8))
        self.buttons.append(b)
        return b

    def _pick_file(self, ent, title, types):
        try:
            self.root.attributes("-topmost", True)
            self.root.bell()
            ft = types if types else [("所有文件", "*.*")]
            p = filedialog.askopenfilename(title=title or "选择文件",
                                           filetypes=ft, parent=self.root)
            self.root.attributes("-topmost", False)
            if p:
                ent.delete(0, "end")
                ent.insert(0, p)
        except Exception as e:
            messagebox.showerror("浏览失败", "无法打开文件对话框：\n" + str(e), parent=self.root)

    def _pick_dir(self, ent):
        try:
            self.root.attributes("-topmost", True)
            self.root.bell()
            p = filedialog.askdirectory(title="选择目录", parent=self.root)
            self.root.attributes("-topmost", False)
            if p:
                ent.delete(0, "end")
                ent.insert(0, p)
        except Exception as e:
            messagebox.showerror("浏览失败", "无法打开目录对话框：\n" + str(e), parent=self.root)

    # ---------------- 页面骨架 ----------------
    def _new_page(self, parent):
        pw = tk.PanedWindow(parent, orient="vertical", bg=C_PANEL, bd=0,
                            sashwidth=7, sashrelief="flat", showhandle=False)
        top = tk.Frame(pw, bg=C_PANEL)
        pw.add(top, stretch="always", minsize=210)
        # 表单可滚动，操作按钮固定在下方，永远看得见
        bar = tk.Frame(top, bg=C_PANEL)
        bar.pack(side="bottom", fill="x", pady=(6, 2))
        sf = ScrollForm(top)
        sf.pack(fill="both", expand=True)
        form = sf.inner
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)
        self.form_list.append(form)
        self.bar_list.append(bar)
        self._cur_actions = bar
        return form, pw

    def _finish_page(self, form, pw, page_idx):
        log = LogPane(pw, paned=pw)
        self.logs.append(log)
        pw.add(log, stretch="never", minsize=96)
        self.pw_list.append(pw)
        pw.bind("<ButtonRelease-1>", lambda e, i=page_idx: self._sash_release(i))
        pw.grid(row=0, column=0, sticky="nsew")
        return pw

    def _place_sash(self, idx):
        """切到该页时把分隔条放到 72%，用户手动拖过就不再干预。"""
        if idx >= len(self.pw_list) or self._sash_user.get(idx):
            return
        pw = self.pw_list[idx]
        pw.update_idletasks()
        h = pw.winfo_height()
        if h < 260:
            return
        need = 300
        if idx < len(self.form_list):
            need = (self.form_list[idx].winfo_reqheight()
                    + self.bar_list[idx].winfo_reqheight() + 26)
        target = max(240, min(int(h * 0.70), need))
        pw.sash_place(0, 0, target)
        self._sash_expect[idx] = pw.sash_coord(0)[1]

    def _sash_release(self, idx):
        if idx >= len(self.pw_list):
            return
        y = self.pw_list[idx].sash_coord(0)[1]
        if abs(y - self._sash_expect.get(idx, -999)) > 4:
            self._sash_user[idx] = True

    def _group(self, form, r, title):
        box = tk.Frame(form, bg=C_PANEL)
        box.grid(row=r, column=0, columnspan=4, sticky="we", pady=(10, 2))
        tk.Label(box, text=title, bg=C_PANEL, fg=C_SECOND, font=F_GROUP).pack(side="left")
        tk.Frame(box, bg=C_LINE, height=1).pack(side="left", fill="x", expand=True,
                                                padx=(10, 0))
        return r + 1

    def _flabel(self, form, r, text, tip=None, col=0):
        lb = tk.Label(form, text=text, bg=C_PANEL, fg=C_SECOND, font=F_LABEL,
                      anchor="e", width=10)
        lb.grid(row=r, column=col, sticky="e", padx=(0, 10), pady=5)
        if tip:
            Tooltip(lb, tip)
        return lb

    def _fentry(self, form, r, value, show=None, col=1, colspan=1, width=None):
        box = tk.Frame(form, bg=C_PANEL)
        box.grid(row=r, column=col, columnspan=colspan, sticky="we", pady=5)
        e = tk.Entry(box, width=width or 40, show=show or "", bg=C_ENTRY, fg=C_TEXT,
                     insertbackground=C_GOLD, relief="flat", font=F_ENTRY,
                     highlightthickness=1, highlightbackground=C_LINE,
                     highlightcolor=C_GOLD)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        e.insert(0, value)
        return e, box

    def _fpick(self, form, r, text, value, title, types, kind="file", tip=None):
        self._flabel(form, r, text, tip)
        box = tk.Frame(form, bg=C_PANEL)
        box.grid(row=r, column=1, columnspan=3, sticky="we", pady=5)
        e = tk.Entry(box, bg=C_ENTRY, fg=C_TEXT, insertbackground=C_GOLD, relief="flat",
                     font=F_ENTRY, highlightthickness=1, highlightbackground=C_LINE,
                     highlightcolor=C_GOLD)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        e.insert(0, value)
        cb = (lambda: self._pick_dir(e)) if kind == "dir" else \
             (lambda: self._pick_file(e, title, types))
        tk.Button(box, text="···", command=cb, bg=C_PANEL_HI, fg=C_SECOND, relief="flat",
                  font=F_BTN, width=3, pady=2, cursor="hand2", bd=0,
                  highlightthickness=1, highlightbackground=C_LINE).pack(
            side="left", padx=(6, 0))
        return e

    def _page_actions(self, form, r, specs):
        bar = getattr(self, "_cur_actions", None)
        if bar is None:
            bar = tk.Frame(form, bg=C_PANEL)
            bar.grid(row=r, column=0, columnspan=4, sticky="w", pady=(14, 4))
        for spec in specs:
            text, cb = spec[0], spec[1]
            primary = spec[2] if len(spec) > 2 else False
            danger = spec[3] if len(spec) > 3 else False
            self._add_btn(bar, text, cb, primary=primary, danger=danger)
        return bar

    def _hint(self, form, r, text, warn=False):
        lb = tk.Label(form, text=text, bg=C_PANEL, fg=(C_GOLD if warn else C_DIM),
                      font=(F_LABEL if warn else F_HINT), anchor="w", justify="left",
                      wraplength=880)
        lb.grid(row=r, column=0, columnspan=4, sticky="w", pady=(2, 1))
        return r + 1

    # ---------------- 页 1：部署 ----------------
    def _page_deploy(self, parent):
        self._building_page = 0
        form, pw = self._new_page(parent)
        c = self.cfg
        r = 0
        r = self._group(form, r, "连接信息")
        self._flabel(form, r, "服务器 IP")
        self.e_host, _ = self._fentry(form, r, c["host"], col=1)
        self._flabel(form, r, "root 密码", col=2, tip="仅保存在本机 s5a3_config.json，不会上传。")
        self.e_pwd, pbox = self._fentry(form, r, c["password"], show="●", col=3)
        tk.Button(pbox, text="显示", command=self._toggle_pwd, bg=C_PANEL_HI, fg=C_DIM,
                  relief="flat", font=F_HINT, width=4, cursor="hand2", bd=0,
                  highlightthickness=1, highlightbackground=C_LINE).pack(
            side="left", padx=(6, 0))
        r += 1
        self._flabel(form, r, "远程仓库", tip="示例：https://github.com/Cec1c/ServerS5A3.git\n必须含 runtime/docker/。")
        self.e_repo, _ = self._fentry(form, r, c["repo"], colspan=3)
        r += 1

        r = self._group(form, r, "本地文件（三选一）")
        self.e_pkg = self._fpick(form, r, "本机源码包", c["local_pkg"],
                                 "选择源码包", [("源码包", "*.tar.gz *.tgz *.zip"),
                                                ("All", "*.*")],
                                 tip="示例：D:\\src\\servers5a3.tar.gz\ngit 仓库源码包，填了就不走远程仓库。")
        r += 1
        self.e_bundle = self._fpick(form, r, "离线安装包", c["bundle_pkg"],
                                    "选择离线安装包",
                                    [("安装包", "*.tar.gz *.tgz"), ("All", "*.*")],
                                    tip="含镜像/数据库/PVF 的官方离线包。\n仅首次安装用，会导库；已有部署会被拒绝。")
        r += 1
        self.e_update = self._fpick(form, r, "更新包", c["update_pkg"],
                                    "选择源码更新包",
                                    [("更新包", "*.tar.gz *.tgz *.zip"), ("All", "*.*")],
                                    tip="日常更新用：上传解压覆盖源码 → 重建镜像 → 重启容器。\n"
                                        "不会改动数据库，也不会覆盖 runtime/docker/data（PVF 等运行数据）。")
        r += 1

        r = self._group(form, r, "服务器路径")
        self._flabel(form, r, "部署路径", tip="默认 /opt/servers5a3。install_gm.sh 写死了这个路径。")
        self.e_path, _ = self._fentry(form, r, c["path"], col=1)
        self._flabel(form, r, "补丁目录", col=2, tip="三个私有补丁的来源，默认 /opt/s5a3-patches。")
        self.e_patch, _ = self._fentry(form, r, c["patch_dir"], col=3)
        r += 1

        r = self._group(form, r, "PVF 与备份")
        self.e_pvf_deploy = self._fpick(form, r, "部署 PVF", c["pvf_deploy"],
                                        "空服务器初始化时上传的 Script.pvf",
                                        [("PVF", "*.pvf"), ("All", "*.*")],
                                        tip="空服务器初始化时上传；服务端已有 PVF 则跳过。")
        r += 1
        self.e_pvf_replace = self._fpick(form, r, "替换 PVF", c["pvf_replace"],
                                         "点「替换 PVF」时上传的 Script.pvf",
                                         [("PVF", "*.pvf"), ("All", "*.*")],
                                         tip="替换前会备份到服务器与本机，校验 SHA256 后才落盘。")
        r += 1
        self.e_backup = self._fpick(form, r, "备份目录", c["backup_dir"],
                                    "本机备份落地目录", [], kind="dir",
                                    tip="PVF 与数据库备份都落这里，留空用 exe 同目录 backups\\。")
        r += 1

        self._page_actions(form, r, [
            ("检测连接", lambda: self._start(self.do_test), False, False),
            ("一键部署", self._confirm_deploy, True, True),
            ("更新服务端", self._confirm_update, False, True),
            ("替换 PVF", self._confirm_pvf, False, True),
            ("查看状态", lambda: self._start(self.do_status), False, False),
        ])
        return self._finish_page(form, pw, 0)

    # ---------------- 页 2：数据库 ----------------
    def _page_db(self, parent):
        self._building_page = 1
        form, pw = self._new_page(parent)
        r = self._group(form, r=0, title="数据库操作")
        r = self._hint(form, r, f"库名 {DB_NAME}　·　备份落服务器 <部署路径>/backups/db 与本机备份目录\\db")
        r = self._hint(form, r, "!  导入 / 恢复前会停止游戏服，玩家会掉线；上传会校验 SHA256，备份为空则中止。",
                       warn=True)
        r = self._hint(form, r, "初始 = 新服首次导入（不先备份）　替换 = 先整库备份再覆盖　恢复 = 从本机备份还原")
        self._page_actions(form, r, [
            ("导入初始库", self._db_import_initial, True, True),
            ("替换数据库", self._db_replace, False, True),
            ("备份数据库", lambda: self._start(self._db_backup), False, False),
            ("恢复备份", self._db_restore, False, True),
        ])
        return self._finish_page(form, pw, 1)

    # ---------------- 页 3：登录器 ----------------
    def _page_launcher(self, parent):
        self._building_page = 2
        form, pw = self._new_page(parent)
        c = self.cfg
        r = self._group(form, 0, "登录器文件")
        self.e_launcher = self._fpick(form, r, "登录器 EXE", c["launcher_exe"],
                                      "玩家用的官方登录器",
                                      [("EXE", "*.exe"), ("All", "*.*")],
                                      tip="示例：D:\\S5A3\\PurpleMagic.exe\n玩家用的官方登录器，用于上传与本地启动。")
        r += 1
        self.e_client = self._fpick(form, r, "客户端目录", c["client_dir"],
                                    "含 DNF.exe 与 Script.pvf", [], kind="dir",
                                    tip="示例：C:\\DNF\\客户端\\DNF90\n用于比对 PVF 与频道表哈希。")
        r += 1
        self.e_channel = self._fpick(form, r, "频道表", c["channel_table"],
                                     "随包 channel_info_120.etc（可留空）",
                                     [("ETC", "*.etc"), ("All", "*.*")],
                                     tip="示例：C:\\DNF\\渠道表\\channel_info_120.etc\n留空则自动找客户端 assets 下的同名表。")
        r += 1

        r = self._group(form, r, "发布设置")
        self._flabel(form, r, "宣告 IP", tip="留空则自动使用「服务器 IP」。")
        self.e_advertise, _ = self._fentry(form, r, c["advertise_ip"], col=1)
        self._flabel(form, r, "发布目录", col=2, tip="网关 release_dir；留空则从网关配置自动探测。")
        self.e_release, _ = self._fentry(form, r, c["release_dir"], col=3)
        r += 1
        r = self._group(form, r, "客户端更新包")
        self._flabel(form, r, "更新包", tip="网盘/直链 URL（http/https），或本机 zip 路径（仅校验不发布）")
        self.e_pack, _ = self._fentry(form, r, c.get("pack_src", ""), colspan=3)
        r += 1
        self._flabel(form, r, "版本号", tip="例：v217-20261121；不能与当前发布相同")
        self.e_packver, _ = self._fentry(form, r, c.get("pack_version", ""), col=1)
        r += 1
        r = self._hint(form, r, "zip 内只允许 client/ 下的文件（登录器 PurpleMagic.exe 可放根）；发布后玩家开登录器自动整包更新")
        r = self._hint(form, r, "上传后玩家从 /launcher/PurpleMagic.exe 下载；发布目录留空会先探测网关配置")
        self._page_actions(form, r, [
            ("上传登录器", self._launcher_upload, False, False),
            ("通讯验证", lambda: self._start(self.do_verify), True, False),
            ("检查通讯文件", lambda: self._start(self.do_profile_check), False, False),
            ("生成通讯文件", self._profile_generate_confirm, False, True),
            ("检查更新包", lambda: self._start(self.do_pack_check), False, False),
            ("发布更新包", self._pack_publish_confirm, False, True),
            ("启动登录器", lambda: self._start(self.do_launcher), False, False),
        ])
        return self._finish_page(form, pw, 2)

    # ---------------- 页 4：GM ----------------
    def _page_gm(self, parent):
        self._building_page = 3
        form, pw = self._new_page(parent)
        r = self._group(form, 0, "GM 后台（PurpleMagic · Docker Compose）")
        self.e_pm_src, _ = self._fentry(form, r, self.cfg.get("pm_src", ""), colspan=3)
        r += 1
        self.e_pm_data, _ = self._fentry(form, r, self.cfg.get("pm_data", ""), colspan=3)
        r += 1
        self.e_pm_repo, _ = self._fentry(form, r, self.cfg.get("pm_repo", ""), colspan=3)
        r += 1
        r = self._hint(form, r, "后台 https://<服务器IP>/console/　登录用 Token（重置后在日志里给一次，并自动复制）")
        r = self._hint(form, r, "「GM 仓库」填了就每次部署前先 fetch + reset 到最新；留空则用服务器上现有代码")
        r = self._hint(form, r, "「部署 GM」= 构建镜像 + 缺失时建索引 + 拉起 gateway/gm/edge；索引已存在会跳过")
        r = self._hint(form, r, "换过 Script.pvf 要点「重建索引」；日常只更新代码就点「部署 GM」")
        self._page_actions(form, r, [
            ("部署 GM", self._pm_deploy_confirm, True, True),
            ("重置管理 Token", self._pm_token_confirm, False, True),
            ("后台状态", lambda: self._start(self.do_gm_status), False, False),
            ("重建索引", self._pm_reindex_confirm, False, True),
            ("打开后台", self._open_panel, False, False),
        ])
        return self._finish_page(form, pw, 3)

    def _show_page(self, idx):
        self.page_index = idx
        names = ("游戏服务端", "数据库管理", "登录器服务端", "GM工具")
        for i, f in enumerate(self.pages):
            if i == idx:
                f.grid(row=0, column=0, sticky="nsew")
            else:
                f.grid_remove()
        for i, b in enumerate(self.nav_buttons):
            b.configure(bg=C_GOLD if i == idx else C_PANEL_HI,
                        fg="#1A1204" if i == idx else C_SECOND)
        if hasattr(self, "st_right"):
            self.st_right.configure(text=names[idx])
        self.root.after(30, lambda: self._place_sash(idx))

    # ---------------- 取值 ----------------
    def _read_form(self):
        def gv(attr, key):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    return w.get().strip()
                except Exception:
                    pass
            return self.cfg.get(key, "")

        cfg = {
            "host": self.e_host.get().strip(),
            "user": "root",
            "password": self.e_pwd.get(),
            "repo": self.e_repo.get().strip(),
            "local_pkg": self.e_pkg.get().strip(),
            "path": self.e_path.get().strip() or "/opt/servers5a3",
            "patch_dir": self.e_patch.get().strip() or "/opt/s5a3-patches",
            "pvf_deploy": self.e_pvf_deploy.get().strip(),
            "pvf_replace": self.e_pvf_replace.get().strip(),
            "backup_dir": self.e_backup.get().strip(),
            "pm_src": self.e_pm_src.get().strip(),
            "pm_data": self.e_pm_data.get().strip(),
            "pm_repo": self.e_pm_repo.get().strip(),
            "launcher_exe": gv("e_launcher", "launcher_exe"),
            "client_dir": gv("e_client", "client_dir"),
            "advertise_ip": gv("e_advertise", "advertise_ip"),
            "channel_table": gv("e_channel", "channel_table"),
            "release_dir": gv("e_release", "release_dir"),
            "bundle_pkg": gv("e_bundle", "bundle_pkg"),
            "update_pkg": gv("e_update", "update_pkg"),
            "pack_src": gv("e_pack", "pack_src"),
            "pack_version": gv("e_packver", "pack_version"),
        }
        self.cfg = cfg
        save_config(cfg)
        return cfg

    # ---------------- 日志 / 状态 ----------------
    def _log(self, msg, tag=None):
        self.log_queue.put((self.log_sink_page, msg, tag))

    def _pump_log(self):
        # 一帧最多处理 MAX_PER_TICK 条，剩下的留到下一帧：
        # docker build 会瞬间刷几千行，一次插完主线程就卡死（曾表现为「未响应」）。
        MAX_PER_TICK = 400
        by_page = {}
        n = 0
        try:
            while n < MAX_PER_TICK:
                page, msg, tag = self.log_queue.get_nowait()
                n += 1
                if not self.logs:
                    continue
                by_page.setdefault(page, []).append((msg, tag))
        except queue.Empty:
            pass
        for page, items in by_page.items():
            pane = self.logs[page] if 0 <= page < len(self.logs) else self.logs[0]
            pane.append_many(items)
        # 界面事件统一在主线程处理（工作线程只入队，不碰 Tk）
        try:
            while True:
                evt = self.ui_queue.get_nowait()
                if evt[0] == "done":
                    self._done(evt[1])
                elif evt[0] == "conn":
                    self._set_conn(evt[1], evt[2] if len(evt) > 2 else None)
        except queue.Empty:
            pass
        self.root.after(90, self._pump_log)

    def _clear_log(self):
        for pane in self.logs:
            pane.clear()

    def _set_conn(self, ok, text=None):
        if not hasattr(self, "st_conn"):
            return
        self.st_conn.configure(text=("● " + (text or "已连接")) if ok else "● 未连接",
                               fg=C_OK if ok else C_DANGER)

    def _set_progress(self, text, color=None):
        if hasattr(self, "st_mid"):
            self.st_mid.configure(text=text, fg=color or C_DIM)

    def _spin_start(self, prefix="处理中"):
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0

        def tick():
            if not self.busy:
                return
            ch = self._spin_frames[self._spin_i % len(self._spin_frames)]
            self._spin_i += 1
            self._set_progress(f"{ch} {prefix}…", C_GOLD)
            self._spin_job = self.root.after(110, tick)

        tick()

    def _spin_stop(self):
        job = getattr(self, "_spin_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._spin_job = None

    def _save_cfg_click(self):
        cfg = self._read_form()
        ok, path, err = save_config(cfg)
        if ok:
            where = os.path.basename(path)
            self._set_progress(f"配置已保存（{where}，含本机密码）", C_OK)
            self._log(f"配置已保存到 {path}", "ok")
            if path == CONFIG_FALLBACK:
                self._log("      exe 目录不可写，已改存到 LOCALAPPDATA", "warn")
        else:
            self._set_progress("配置保存失败（不影响本次使用）", C_DANGER)
            self._log(f"[错误] 配置保存失败：{err or '未知原因'}", "err")
            self._log("      目录权限不足时可把 exe 放到可写目录再试", "err")

    def _reset_cfg_click(self):
        if not messagebox.askyesno("重置配置",
                                   "将清空本机保存的全部字段（IP、密码、路径等），恢复默认值。\n"
                                   "继续？", parent=self.root):
            return
        removed = []
        for path in (CONFIG_PATH, CONFIG_FALLBACK):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed.append(os.path.basename(path))
            except Exception:
                pass
        messagebox.showinfo("已重置",
                            "已删除：" + ("、".join(removed) if removed else "（无配置文件）")
                            + "\n重启工具后生效。", parent=self.root)

    def _toggle_pwd(self):
        self.e_pwd.configure(show="" if self.e_pwd.cget("show") else "●")


    # ---------------- 任务调度 ----------------
    def _ask_danger(self, title, lines, target, typed=True):
        dlg = DangerDialog(self.root, title, lines, target, typed)
        self.root.wait_window(dlg)
        return dlg.ok

    def _start(self, fn, confirm=None, danger=None):
        if self.busy:
            messagebox.showinfo("提示", "当前有任务在执行", parent=self.root)
            return
        cfg = self._read_form()
        if not cfg["host"] or not cfg["password"]:
            messagebox.showwarning("缺少配置", "请填写服务器 IP 和 root 密码", parent=self.root)
            return
        if danger:
            if not self._ask_danger(*danger):
                self._active_btn = None
                return
        elif confirm:
            if not messagebox.askyesno(confirm[0], confirm[1], parent=self.root):
                self._active_btn = None
                return
        self.busy = True
        self._t0 = time.time()
        self.log_sink_page = self.log_target
        self._last_line = None
        self._set_progress("处理中…", C_GOLD)
        if self._active_btn:
            b, orig = self._active_btn
            try:
                b.configure(text="处理中…", state="disabled")
            except Exception:
                pass
        for b in self.buttons:
            b.configure(state="disabled")
        self._spin_start()
        threading.Thread(target=self._wrap, args=(fn, cfg), daemon=True).start()

    def _wrap(self, fn, cfg):
        ok = False
        try:
            fn(cfg)
            ok = True
        except Exception as e:
            self._log(f"[错误] {e}", "err")
        finally:
            self.busy = False
            self.ui_queue.put(("done", ok))

    def _done(self, ok):
        self._spin_stop()
        if self._active_btn:
            b, orig = self._active_btn
            try:
                b.configure(text=orig, state="normal")
            except Exception:
                pass
            self._active_btn = None
        for b in self.buttons:
            b.configure(state="normal")
        dur = time.time() - getattr(self, "_t0", time.time())
        self._set_progress(("完成" if ok else "失败，见日志") + f"  用时 {dur:.1f}s",
                           C_OK if ok else C_DANGER)
        self._set_conn(getattr(self, "_connected", False))

    def _ssh(self, cfg):
        cli = SSHClient(cfg["host"], cfg["user"], cfg["password"])
        self._log(f"      正在连接 {cfg['host']} …")
        try:
            cli.connect()
        except Exception:
            self._connected = False
            self.ui_queue.put(("conn", False))
            raise
        self._connected = True
        self.ui_queue.put(("conn", True, f"已连接 {cfg['host']}"))
        return cli

    def _line_cb(self, line):
        line = strip_ansi(line)
        if not line.strip():
            return
        # 进度行会反复刷同一条内容，重复的直接丢掉，省一次文本插入
        if line == getattr(self, "_last_line", None):
            return
        self._last_line = line
        if self._RE_ERR.search(line):
            tag = "err"
        elif self._RE_WARN.search(line):
            tag = "warn"
        elif line.startswith("["):
            tag = "info"
        elif self._RE_OK.search(line):
            tag = "ok"
        else:
            tag = "info"
        self._log(line, tag)

    def _rp(self, cfg, *parts):
        return posixpath.join(cfg["path"], *parts)

    def _backup_root(self, cfg):
        d = (cfg.get("backup_dir") or "").strip() or os.path.join(APP_DIR, "backups")
        d = os.path.abspath(os.path.expanduser(d))
        os.makedirs(d, exist_ok=True)
        return d

    def _prog_cb(self, label, step=10):
        state = {"pct": -1}

        def cb(done, total):
            pct = int(done * 100 / total) if total else 0
            if pct >= state["pct"] + step or pct == 100:
                state["pct"] = pct
                self._log(f"      {label} {pct}%")

        return cb

    def _mkdir(self, cli, path):
        cli.capture(f"mkdir -p {q(path)}")

    # ---------------- 上传 / 备份 ----------------
    def _put_verified(self, cli, local, final_remote, label, local_sha=None):
        """先传 .uploading，校验大小 + SHA256 通过后再 mv 到位；保留原文件权限/属主。"""
        size = os.path.getsize(local)
        if local_sha is None:
            self._log("      计算本机 SHA256 …")
            local_sha = sha256_file(local, self._prog_cb("校验", step=25))
        self._log(f"      本机 {human_size(size)}  sha256={local_sha[:16]}…")
        # 记录旧文件权限/属主，替换后还原（容器内非 root 用户要能读）
        _, meta = cli.capture(
            f"if [ -f {q(final_remote)} ]; then stat -c '%a %u:%g' {q(final_remote)}; fi")
        meta = (meta or "").strip()
        tmp = final_remote + ".uploading"
        try:
            cli.sftp_put(local, tmp, self._prog_cb("上传"))
            _, out = cli.capture(f"stat -c %s {q(tmp)}")
            rsize = int(out.strip()) if out.strip().isdigit() else -1
            _, rsha = cli.capture(f"sha256sum {q(tmp)} | {AWK_FIRST}")
            rsha = (rsha or "").strip()
            if rsize != size or rsha != local_sha:
                raise RuntimeError(
                    f"{label} 上传校验失败：本机 {size}B/{local_sha[:12]}，"
                    f"远端 {rsize}B/{(rsha[:12] or '未知')}；旧文件未改动")
            cli.capture(f"mv -f {q(tmp)} {q(final_remote)}")
            if meta and len(meta.split()) == 2:
                mode, owner = meta.split()
                cli.capture(f"chmod {mode} {q(final_remote)}; "
                            f"chown {owner} {q(final_remote)} 2>/dev/null || true")
                self._log(f"      已还原权限 {mode} / 属主 {owner}")
            self._log("      校验通过（大小 + SHA256），已落盘", "ok")
        except Exception:
            try:
                cli.capture(f"rm -f {q(tmp)}")
            except Exception:
                pass
            raise

    def _backup_pvf(self, cli, cfg):
        remote = self._rp(cfg, PVF_REL)
        bkdir = self._rp(cfg, "backups/pvf")
        bk = posixpath.join(bkdir, "Script.pvf." + time.strftime("%Y%m%d-%H%M%S") + ".bak")
        _, out = cli.capture(
            f"mkdir -p {q(bkdir)}; "
            f"if [ -f {q(remote)} ]; then cp -f {q(remote)} {q(bk)} && echo {q(bk)}; "
            f"else echo NONE; fi",
            timeout=900)
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if line == "NONE":
                self._log("      服务端没有旧 PVF，跳过备份")
                return None
            if line.startswith(cfg["path"]):
                self._log(f"      服务端备份：{line}")
                return line
        self._log("[警告] 备份命令输出异常，本机不生成副本", "err")
        return None

    def _download(self, cli, remote, cfg, sub):
        d = os.path.join(self._backup_root(cfg), sub)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, posixpath.basename(remote))
        self._log(f"      下载到 {dst}")
        cli.sftp_get(remote, dst, self._prog_cb("下载"))
        self._log(f"      本机备份已保存（{human_size(os.path.getsize(dst))}）", "ok")
        return dst

    # ---------------- 部署流程 ----------------
    def _source_mode(self, cfg):
        bundle = (cfg.get("bundle_pkg") or "").strip()
        if bundle:
            if not os.path.isfile(bundle):
                raise RuntimeError(f"离线安装包不存在：{bundle}")
            if not bundle.lower().endswith((".tar.gz", ".tgz")):
                raise RuntimeError("离线安装包只支持 .tar.gz / .tgz")
            return "bundle"
        upd = (cfg.get("update_pkg") or "").strip()
        if upd:
            if not os.path.isfile(upd):
                raise RuntimeError(f"更新包不存在：{upd}")
            if not upd.lower().endswith((".tar.gz", ".tgz", ".zip")):
                raise RuntimeError("更新包只支持 .tar.gz / .tgz / .zip")
            return "update"
        lp = (cfg.get("local_pkg") or "").strip()
        if lp:
            if not os.path.isfile(lp):
                raise RuntimeError(f"本机源码包不存在：{lp}")
            low = lp.lower()
            if low.endswith((".tar.gz", ".tgz", ".zip")):
                return "pkg"
            raise RuntimeError("源码包只支持 .tar.gz / .tgz / .zip（未压缩的 .tar 不支持）")
        if not (cfg.get("repo") or "").strip():
            raise RuntimeError("请填远程仓库、或选本机源码包、离线安装包、更新包")
        return "git"

    def _preflight(self, cli, cfg):
        mode = self._source_mode(cfg)
        parent = posixpath.dirname(cfg["path"]) or "/"
        need = ["docker", "tar"]
        if mode == "git":
            need.append("git")
        elif mode == "update" and (cfg.get("update_pkg") or "").lower().endswith(".zip"):
            need.append("unzip")
        elif cfg["local_pkg"].lower().endswith(".zip"):
            need.append("unzip")
        checks = ""
        for b in need:
            checks += (f'command -v {b} >/dev/null 2>&1 || '
                       f'{{ echo "[错误] 服务器没有 {b}"; exit 1; }}\n')
        script = f"""
set -e
{checks}mkdir -p {q(parent)}
echo "      docker：$(docker --version)"
if docker compose version >/dev/null 2>&1; then
  echo "      compose：$(docker compose version | head -1)"
else
  echo "[错误] 缺少 docker compose v2 子命令（只有 docker-compose v1 不行）"
  exit 1
fi
MEM=$(free -m | awk '/^Mem:/{{print $2}}')
echo "      内存：${{MEM}} MB"
if [ "${{MEM:-0}}" -lt 5800 ]; then
  echo "[警告] 内存低于建议的 6GB，游戏服可能被 OOM 杀掉"
fi
echo "      磁盘：$(df -h {q(parent)} | tail -1)"
"""
        rc = cli.run(script, self._line_cb, timeout=120)
        if rc != 0:
            raise RuntimeError("服务器环境检查未通过")

    def _restore_patches(self, cli, cfg):
        pd = (cfg.get("patch_dir") or "/opt/s5a3-patches").strip()
        script = f"""
set -e
set -o pipefail 2>/dev/null || true
cd {q(cfg['path'])}
PD={q(pd)}
if [ ! -d "$PD" ]; then
  echo "[警告] 补丁目录 $PD 不存在，跳过补丁恢复"
  echo "       若私有补丁已提交进仓库可忽略；否则内存看护失效会导致 RSS 涨到 5-6GB"
else
  CNT=0
  for f in "$PD"/*.go; do
    [ -f "$f" ] || continue
    N=$(basename "$f")
    T=$(find . -name "$N" -not -path "./.git/*" | head -1)
    if [ -z "$T" ]; then
      # 私有补丁多为「新增文件」，上游仓库里本没有同名文件：
      # 按 package 声明推断落点（dnfbridge / main）。
      PKG=$(grep -m1 '^package ' "$f" | awk '{{print $2}}')
      case "$PKG" in
        dnfbridge)
          D=$(find . -type d -path "*/internal/services/dnfbridge" -not -path "./.git/*" | head -1) ;;
        main)
          D=$(find . -type d -path "*/cmd/server/dnf90" -not -path "./.git/*" | head -1) ;;
        *)
          D="" ;;
      esac
      if [ -n "$D" ]; then
        T="$D/$N"
      fi
    fi
    if [ -n "$T" ]; then
      cp -f "$f" "$T"
      echo "      已恢复 $N -> $T"
      CNT=$((CNT+1))
    else
      echo "[错误] 源码里找不到 $N 的落点（package 无法匹配），私有补丁无法恢复"
      exit 1
    fi
  done
  echo "      补丁恢复数量：$CNT"
  # *.patch 以前只会被复制却从不执行，结果补丁目录里那份改了上游文件的补丁
  # （account_edit_admission.go / character_edit_admission.go）每次更新都被静默丢掉。
  PCNT=0
  for P in "$PD"/*.patch; do
    [ -f "$P" ] || continue
    PN=$(basename "$P")
    if POUT=$(patch -p1 --forward --batch -i "$P" 2>&1); then
      echo "      已应用补丁 $PN"
      PCNT=$((PCNT+1))
    else
      case "$POUT" in
        *"previously applied"*|*"Reversed (or previously applied)"*)
          echo "      $PN 已是应用状态，跳过" ;;
        *)
          echo "$POUT" | tail -20
          echo "[错误] 补丁 $PN 打不上（上面是 patch 输出），已中止，未构建、未重启"
          exit 1 ;;
      esac
    fi
  done
  echo "      补丁文件应用：$PCNT"
fi
if ! find . -name 's5_mem_guard.go' -not -path './.git/*' | grep -q .; then
  echo "[错误] 源码树里没有 s5_mem_guard.go —— 构建会让容器 RSS 涨到 5-6GB，已拒绝构建"
  exit 1
fi
echo "私有补丁就位"
"""
        rc = cli.run(script, self._line_cb, timeout=300)
        if rc != 0:
            raise RuntimeError("私有补丁恢复失败，已中止（未构建、未重启）")

    def _fetch_sources(self, cli, cfg):
        p = cfg["path"]
        if self._source_mode(cfg) == "git":
            script = f"""
set -e
if [ -d {q(p)}/.git ]; then
  cd {q(p)}
  echo "      已有仓库：fetch + reset"
  git fetch --all --prune
  git reset --hard origin/HEAD 2>/dev/null || git reset --hard origin/main 2>/dev/null || git reset --hard origin/master
  echo "      当前提交：$(git log --oneline -1)"
else
  if [ -d {q(p)} ] && [ -n "$(ls -A {q(p)} 2>/dev/null)" ]; then
    echo "[错误] 目标目录已存在且不是 git 仓库（可能是上次用源码包部署的）"
    echo "       请改用「本机源码包」方式，或先清空该目录再部署"
    exit 1
  fi
  mkdir -p "$(dirname {q(p)})"
  git clone {q(cfg['repo'])} {q(p)} >/dev/null 2>&1 || {{ echo "[错误] git clone 失败"; exit 1; }}
  cd {q(p)}
  echo "      当前提交：$(git log --oneline -1)"
fi
echo "[警告] 已 git reset --hard，服务器上未提交的改动被丢弃（私有补丁随后自动恢复）"
"""
            rc = cli.run(script, self._line_cb, timeout=1800)
            if rc != 0:
                raise RuntimeError("获取源码失败")
            return

        # ---- 本机源码包模式 ----
        local = cfg["local_pkg"]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ext = ".zip" if local.lower().endswith(".zip") else ".tar.gz"
        tmp = f"/tmp/s5a3-src-{stamp}{ext}"
        stage = f"/tmp/s5a3-src-{stamp}"
        self._log(f"      上传源码包（{human_size(os.path.getsize(local))}）")
        cli.sftp_put(local, tmp, self._prog_cb("上传"))
        lsha = sha256_file(local, self._prog_cb("校验", step=25))
        _, rsha = cli.capture(f"sha256sum {q(tmp)} | {AWK_FIRST}")
        if (rsha or "").strip() != lsha:
            raise RuntimeError("源码包上传校验失败，已中止")
        script = f"""
set -e
set -o pipefail 2>/dev/null || true
TMP={q(tmp)}
STAGE={q(stage)}
TARGET={q(p)}
rm -rf "$STAGE"; mkdir -p "$STAGE"
if echo "$TMP" | grep -q '\\.zip$'; then
  unzip -q -o "$TMP" -d "$STAGE" || {{ echo "[错误] 解压失败"; exit 1; }}
else
  tar -xzf "$TMP" -C "$STAGE" || {{ echo "[错误] 解压失败"; exit 1; }}
fi
CNT=$(find "$STAGE" -mindepth 1 -maxdepth 1 | wc -l)
if [ "$CNT" -eq 1 ] && [ -d "$(find "$STAGE" -mindepth 1 -maxdepth 1)" ]; then
  SRC=$(find "$STAGE" -mindepth 1 -maxdepth 1)
else
  SRC="$STAGE"
fi
echo "      包根目录：$SRC"
if [ ! -f "$SRC/runtime/docker/compose.json" ]; then
  echo "[错误] 包里没有 runtime/docker/compose.json，不是完整部署仓库"
  exit 1
fi
mkdir -p "$TARGET"
EXCL="--exclude=./.git"
if [ -f "$TARGET/runtime/docker/data/Script.pvf" ]; then
  echo "      服务端已有 PVF，保留 runtime/docker/data 不被覆盖"
  EXCL="$EXCL --exclude=./runtime/docker/data"
fi
tar -C "$SRC" -cf - $EXCL . | tar -C "$TARGET" -xf -
[ -f "$TARGET/runtime/docker/compose.json" ] || {{ echo "[错误] 覆盖后仍缺 compose.json"; exit 1; }}
echo "      源码已覆盖到 $TARGET"
rm -rf "$STAGE" "$TMP"
"""
        rc = cli.run(script, self._line_cb, timeout=1800)
        if rc != 0:
            raise RuntimeError("源码包解压/覆盖失败")

    def _check_runtime(self, cli, cfg):
        """仓库必需文件自检：缺一个后面必然失败，提前拦下来。"""
        p = cfg["path"]
        need = [COMPOSE, "runtime/docker/server.env",
                "runtime/docker/database.env", "runtime/docker/Dockerfile.deploy"]
        missing = []
        for rel in need:
            rc, _ = cli.capture(f"test -f {q(posixpath.join(p, rel))}")
            if rc != 0:
                missing.append(rel)
        if missing:
            raise RuntimeError(
                "仓库缺少必需文件：" + "、".join(missing) + "（部署会失败）")
        self._log("      必需文件齐全", "ok")

    def _ensure_pvf(self, cli, cfg):
        remote = self._rp(cfg, PVF_REL)
        _, out = cli.capture(
            f"if [ -f {q(remote)} ]; then stat -c %s {q(remote)}; else echo 0; fi")
        try:
            size = int((out or "0").strip() or 0)
        except ValueError:
            size = 0
        if size > 0:
            self._log(f"      服务端已有 PVF（{human_size(size)}），不覆盖")
            return
        local = (cfg.get("pvf_deploy") or "").strip()
        if not local or not os.path.isfile(local):
            self._log("[警告] 服务端没有 Script.pvf，且未填「部署 PVF」", "err")
            self._log("       容器读不到 PVF 会一直 unhealthy；部署完请用「替换 PVF」补上", "err")
            return
        self._mkdir(cli, posixpath.dirname(remote))
        self._log(f"      上传部署 PVF：{os.path.basename(local)}"
                  f"（{human_size(os.path.getsize(local))}）")
        self._put_verified(cli, local, remote, "部署 PVF")

    def _build_and_up(self, cli, cfg, force):
        flag = "--force-recreate" if force else ""
        script = f"""
set -e
cd {q(cfg['path'])}
docker tag servers5a3:launcher servers5a3:rollback-$(date +%Y%m%d%H%M%S) 2>/dev/null || true
echo "      构建镜像（首次 2-5 分钟）"
docker build -t servers5a3:launcher -f runtime/docker/Dockerfile.deploy . || {{
  echo "[错误] 镜像构建失败，运行中的容器未受影响"
  echo "       现存可回滚镜像："
  docker images servers5a3 --format '{{{{.Repository}}}}:{{{{.Tag}}}}' | head -5
  exit 1
}}
echo "      启动容器"
docker compose -p servers5a3 -f {COMPOSE} up -d {flag}
docker compose -p servers5a3 -f {COMPOSE} ps
"""
        rc = cli.run(script, self._line_cb, timeout=3600)
        if rc != 0:
            raise RuntimeError("构建或启动失败，见上方日志")

    def _wait_healthy(self, cli, cfg, timeout=300, interval=15):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            _, out = cli.capture(
                f"docker inspect -f {INSPECT_HEALTH} {SERVER_C} 2>/dev/null")
            st = (out or "").strip()
            if st != last:
                self._log(f"      服务状态：{st or '未知'}")
                last = st
            if st in ("healthy", "running"):
                self._log("      服务已就绪", "ok")
                return True
            if st in ("unhealthy", "exited", "dead"):
                self._log(f"[警告] 服务状态 {st}，请执行 docker logs --tail 100 {SERVER_C}", "err")
                return False
            time.sleep(interval)
        self._log("[警告] 等待超时，服务可能仍在加载 PVF（首次 1-3 分钟）", "err")
        return False

    def _deploy_flow(self, cfg, force, title):
        mode = "本机源码包" if self._source_mode(cfg) == "pkg" else "远程仓库"
        self._log(f"== {title} -> {cfg['path']}（源码：{mode}）==", "gold")
        cli = self._ssh(cfg)
        try:
            self._log("[1/7] 服务器环境检查", "gold")
            self._preflight(cli, cfg)
            self._log("[2/7] 获取源码", "gold")
            self._fetch_sources(cli, cfg)
            self._log("[3/7] 检查 runtime 必需文件", "gold")
            self._check_runtime(cli, cfg)
            self._log("[4/7] 恢复私有补丁", "gold")
            self._restore_patches(cli, cfg)
            self._log("[5/7] 准备 PVF", "gold")
            self._ensure_pvf(cli, cfg)
            self._log("[6/7] 构建镜像 + 启动容器", "gold")
            self._build_and_up(cli, cfg, force)
            self._log("[7/7] 等待服务就绪", "gold")
            self._wait_healthy(cli, cfg)
            self._log(f"== {title}结束 ==", "ok")
        finally:
            cli.close()

    # ---------------- 网关 / 登录器 / 通讯验证 ----------------
    def _gm_profile(self, cli):
        """读面板侧 profile.json。

        位置随 install_gm.sh 版本变过：早期放 data/，现在放 inputs/（settings.json 的
        approvedProfile 指向它），另有 data/private/player-profile.json 一份副本。
        写死一个路径就会把「已装好」误报成「读不到」。
        """
        for rel in ("data/profile.json", "inputs/profile.json",
                    "data/private/player-profile.json"):
            txt = self._remote_text(cli, posixpath.join(GM_BASE, rel))
            if not txt:
                continue
            try:
                prof = json.loads(txt)
            except Exception:
                continue
            if isinstance(prof, dict) and prof:
                return rel, prof
        return "", {}

    def _remote_sha(self, cli, path):
        rc, out = cli.capture(f"sha256sum {q(path)} 2>/dev/null | {AWK_FIRST}")
        val = (out or "").strip()
        return val if re.fullmatch(r"[0-9a-f]{64}", val or "") else ""

    def _remote_text(self, cli, path, timeout=60):
        rc, out = cli.capture(f"cat {q(path)} 2>/dev/null", timeout=timeout)
        return out if rc == 0 else ""

    def _local_client_file(self, cfg, *parts):
        d = (cfg.get("client_dir") or "").strip()
        if not d:
            return ""
        fp = os.path.join(d, *parts)
        return fp if os.path.isfile(fp) else ""

    # ---------------- 动作 ----------------
    def do_test(self, cfg):
        self._log(f"== 连接 {cfg['host']} ==", "gold")
        cli = self._ssh(cfg)
        try:
            script = f"""
echo "      主机：$(hostname)  $(uname -m)"
echo "      系统：$(head -1 /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '\\"')"
echo "      docker：$(docker --version 2>&1)"
if docker compose version >/dev/null 2>&1; then
  echo "      compose：$(docker compose version | head -1)"
else
  echo "[警告] 缺少 docker compose v2 子命令"
fi
echo "      git：$(git --version 2>&1)"
MEM=$(free -m | awk '/^Mem:/{{print $2}}')
echo "      内存：${{MEM}} MB"
if [ "${{MEM:-0}}" -lt 5800 ]; then echo "[警告] 内存低于建议的 6GB"; fi
echo "      磁盘：$(df -h / | tail -1)"
if [ -d {q(cfg['path'])} ]; then echo "      部署目录：已存在 {cfg['path']}"; else echo "      部署目录：未创建"; fi
if [ -d {q(cfg['patch_dir'])} ]; then echo "      补丁目录：已存在"; else echo "[警告] 补丁目录不存在 {cfg['patch_dir']}"; fi
"""
            cli.run(script, self._line_cb, timeout=60)
            self._log("== 检测完成 ==", "ok")
        finally:
            cli.close()

    def _confirm_deploy(self):
        cfg = self._read_form()
        src = ("离线安装包：" + os.path.basename(cfg["bundle_pkg"])) if cfg.get("bundle_pkg") else (
            ("本机源码包：" + os.path.basename(cfg["local_pkg"])) if cfg.get("local_pkg") else
            ("远程仓库：" + (cfg.get("repo") or "")))
        self._start(self.do_deploy, danger=(
            "一键部署",
            ["将重建镜像并启动容器，服务器上未提交的改动会被丢弃。",
             "私有补丁会自动从补丁目录恢复；数据库不会被覆盖。",
             src],
            cfg["path"]))

    def _confirm_update(self):
        cfg = self._read_form()
        upd = (cfg.get("update_pkg") or "").strip()
        if upd:
            lines = ["将上传更新包并覆盖服务器源码，然后重建镜像、重启容器。",
                     f"更新包：{os.path.basename(upd)}",
                     "数据库不动；runtime/docker/data（PVF 等）不动。",
                     "私有补丁会从补丁目录自动恢复。"]
        else:
            lines = ["将拉取/覆盖源码、重建镜像并强制重建容器。",
                     "服务器上未提交的改动会被丢弃。",
                     "离线安装包模式不会执行本操作（会重导数据库）。"]
        self._start(self.do_update, danger=("更新服务端", lines, cfg["path"]))

    def _confirm_pvf(self):
        cfg = self._read_form()
        target = cfg["pvf_replace"] or "（未选择替换 PVF）"
        self._start(self.do_pvf, danger=(
            "替换 PVF",
            ["将覆盖服务器上的 Script.pvf，游戏服会中断约 30 秒。",
             f"新文件：{os.path.basename(target) if os.path.isfile(target) else target}",
             "替换前会自动备份到服务器与本机，并校验 SHA256。"],
            posixpath.join(cfg["path"], PVF_REL)))

    def do_deploy(self, cfg):
        mode = self._source_mode(cfg)
        if mode == "bundle":
            self._deploy_bundle(cfg)
        elif mode == "update":
            self._update_from_pkg(cfg)
        else:
            self._deploy_flow(cfg, force=False, title="一键部署")

    def do_update(self, cfg):
        # 填了更新包就优先走「更新包」路径（不动数据库）
        if (cfg.get("update_pkg") or "").strip():
            self._update_from_pkg(cfg)
            return
        if self._source_mode(cfg) == "bundle":
            raise RuntimeError(
                "离线包模式不支持「更新服务端」：它会重导数据库覆盖玩家数据。"
                "日常更新请用「更新包」；换 PVF 用「游戏服务端」页的「替换 PVF」")
        self._deploy_flow(cfg, force=True, title="更新服务端")

    def _update_from_pkg(self, cfg):
        """更新包：上传 → 解压覆盖源码（保留 data/、不动数据库）→ 重建镜像 → 重启。"""
        local = (cfg.get("update_pkg") or "").strip()
        if not local or not os.path.isfile(local):
            raise RuntimeError("请先选「更新包」")
        p = cfg["path"]
        compose = posixpath.join(p, COMPOSE)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ext = ".zip" if local.lower().endswith(".zip") else ".tar.gz"
        tmp = f"/tmp/s5a3-update-{stamp}{ext}"
        stage = f"/tmp/s5a3-update-{stamp}"
        self._log(f"== 更新包部署（{human_size(os.path.getsize(local))}）-> {p} ==", "gold")
        self._log("      只更新源码并重建镜像；数据库不动，runtime/docker/data 不动", None)
        cli = self._ssh(cfg)
        try:
            self._log("[1/6] 检查环境", "gold")
            self._preflight(cli, cfg)
            rc, _ = cli.capture(f"test -f {q(compose)}")
            if rc != 0:
                raise RuntimeError("服务端还没部署过（没有 compose.json），请先用「一键部署」")

            self._log("[2/6] 上传更新包", "gold")
            cli.sftp_put(local, tmp, self._prog_cb("上传", 5))
            lsha = sha256_file(local, self._prog_cb("校验", step=25))
            _, rsha = cli.capture(f"sha256sum {q(tmp)} | {AWK_FIRST}", timeout=600)
            if (rsha or "").strip() != lsha:
                raise RuntimeError("更新包上传校验失败，已中止")
            self._log("      校验通过（SHA256）", "ok")

            self._log("[3/6] 解压并覆盖源码（保留 runtime/docker/data）", "gold")
            script = f"""
set -e
set -o pipefail 2>/dev/null || true
TMP={q(tmp)}
STAGE={q(stage)}
TARGET={q(p)}
rm -rf "$STAGE"; mkdir -p "$STAGE"
if echo "$TMP" | grep -q '\\.zip$'; then
  unzip -q -o "$TMP" -d "$STAGE" || {{ echo "[错误] 解压失败"; exit 1; }}
else
  tar -xzf "$TMP" -C "$STAGE" || {{ echo "[错误] 解压失败"; exit 1; }}
fi
CNT=$(find "$STAGE" -mindepth 1 -maxdepth 1 | wc -l)
if [ "$CNT" -eq 1 ] && [ -d "$(find "$STAGE" -mindepth 1 -maxdepth 1)" ]; then
  SRC=$(find "$STAGE" -mindepth 1 -maxdepth 1)
else
  SRC="$STAGE"
fi
echo "      包根目录：$SRC"
if [ ! -d "$SRC/go-server" ] && [ ! -f "$SRC/runtime/docker/compose.json" ]; then
  echo "[错误] 包里既没有 go-server/ 也没有 runtime/docker，不像是源码更新包"
  exit 1
fi
EXCL="--exclude=./.git --exclude=./runtime/docker/data"
tar -C "$SRC" -cf - $EXCL . | tar -C "$TARGET" -xf -
echo "      源码已覆盖（runtime/docker/data 未被触碰）"
"""
            rc = cli.run(script, self._line_cb, timeout=1800)
            if rc != 0:
                raise RuntimeError("更新包解压/覆盖失败")

            self._log("[4/6] 检查必需文件 + 恢复私有补丁", "gold")
            self._check_runtime(cli, cfg)
            self._restore_patches(cli, cfg)

            self._log("[5/6] 重建镜像并重启容器", "gold")
            self._build_and_up(cli, cfg, force=True)

            self._log("[6/6] 等待服务就绪", "gold")
            self._wait_healthy(cli, cfg, timeout=600)
            cli.capture(f"rm -rf {q(tmp)} {q(stage)}", timeout=300)
            self._log("== 更新完成（数据库未改动）==", "ok")
        finally:
            cli.close()

    def _finish_bundle_check(self, cli, cfg, p, compose, adv):
        self._log("[5/8] 容器状态", "gold")
        cli.run(f"docker compose -p servers5a3 -f {q(compose)} ps",
                self._line_cb, timeout=180)
        self._log("[6/8] 等待服务健康（PVF 首次加载 1-3 分钟）", "gold")
        self._wait_healthy(cli, cfg, timeout=600)
        self._log("[7/8] 账号 API", "gold")
        _, code = cli.capture(
            "curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:17002/api/status")
        code = (code or "").strip()
        if code == "200":
            self._log("      /api/status 200", "ok")
        else:
            self._log(f"[警告] /api/status 返回 {code or '无响应'}", "err")

    # ---------------- 离线安装包（首次安装） ----------------
    def _bundle_state(self, cli, cfg):
        """判断服务端是「已装」「半装」还是「未装」。"""
        dbenv = self._rp(cfg, DBENV_REL)
        compose = self._rp(cfg, COMPOSE)
        rc, _ = cli.capture(f"test -f {q(compose)}")
        if rc != 0:
            return "absent"
        tables = 0
        rc, out = cli.capture(
            f"PW=$(grep '^MARIADB_ROOT_PASSWORD=' {q(dbenv)} | cut -d= -f2-); "
            f"docker exec {DB_C} mariadb -uroot -p\"$PW\" -N -e "
            f"\"SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema='{DB_NAME}'\" 2>/dev/null")
        if rc == 0 and (out or "").strip().isdigit():
            tables = int((out or "0").strip())
        return "installed" if tables > 0 else "partial"

    def _find_remote_bundle(self, cli):
        _, out = cli.capture(
            "ls -d /root/s5a3-bundle-*/s5a3-deploy-bundle 2>/dev/null | tail -1")
        return (out or "").strip()

    def _bundle_resume(self, cli, cfg, bundle_dir):
        """接着做 install.sh 的第 7-8 步（起库、导库、起服）。"""
        p = cfg["path"]
        compose = posixpath.join(p, COMPOSE)
        self._log("[续做] 检查占位符是否已全部写入", "gold")
        _, left = cli.capture(
            f"grep -rlI '__PUBLIC_IP__' {q(p)}/runtime/docker 2>/dev/null | wc -l")
        if (left or "").strip() != "0":
            raise RuntimeError(f"仍有 {left.strip()} 个文件含 __PUBLIC_IP__，不能续做")
        if not bundle_dir or not bundle_dir.startswith("/root/"):
            raise RuntimeError("找不到已解压的安装包目录，无法续做；请清理后重试")

        script = f"""
set -o pipefail 2>/dev/null || true
cd {q(p)}
PW=$(grep '^MARIADB_ROOT_PASSWORD=' runtime/docker/database.env | cut -d= -f2-)
T=$(docker exec {DB_C} mariadb -uroot -p"$PW" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{DB_NAME}'" 2>/dev/null)
echo "      当前 {DB_NAME} 表数：${{T:-0}}"
if [ "${{T:-0}}" -gt 0 ]; then
  echo "      库里已有数据，跳过导入"
else
  echo "[7/8] 启动数据库并等待 healthy"
  docker compose -p servers5a3 -f runtime/docker/compose.json up -d db
  for i in $(seq 1 90); do
    docker exec {DB_C} healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1 && {{ echo "      db ready"; break; }}
    sleep 2
  done
  echo "[7/8] 导入 {DB_NAME}"
  gunzip -c {q(bundle_dir)}/db/dnf_local.sql.gz | docker exec -i {DB_C} mariadb -uroot -p"$PW" {DB_NAME} || {{ echo "[错误] 导入失败"; exit 1; }}
  T2=$(docker exec {DB_C} mariadb -uroot -p"$PW" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{DB_NAME}'")
  echo "      导入后表数：$T2"
fi
echo "[8/8] 启动游戏服"
docker compose -p servers5a3 -f runtime/docker/compose.json up -d
"""
        rc = cli.run(script, self._line_cb, timeout=3600)
        if rc != 0:
            raise RuntimeError("续做第 7-8 步失败，见上方日志")


    def _deploy_bundle(self, cfg):
        local = (cfg.get("bundle_pkg") or "").strip()
        if not local or not os.path.isfile(local):
            raise RuntimeError("请先选「离线安装包」")
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        remote_tar = f"/root/s5a3-bundle-{stamp}.tar.gz"
        remote_dir = f"/root/s5a3-bundle-{stamp}"
        p = cfg["path"]
        compose = posixpath.join(p, COMPOSE)
        size = os.path.getsize(local)
        self._log(f"== 离线包部署（{human_size(size)}）-> {adv}:7001 ==", "gold")
        self._log("      离线包会加载镜像、还原 runtime、导入数据库、启动容器", None)
        cli = self._ssh(cfg)
        try:
            self._log("[1/8] 检查环境", "gold")
            self._preflight(cli, cfg)
            state = self._bundle_state(cli, cfg)
            if state == "installed":
                raise RuntimeError(
                    "服务端已是完整部署（库里有数据）。离线包会重建源码并重导数据库，"
                    "已拒绝执行；重装请先手工清理 " + p)
            if state == "partial":
                self._log("[警告] 检测到上次安装未完成（有源码、库里没数据）", "err")
                bd = self._find_remote_bundle(cli)
                if not bd:
                    raise RuntimeError(
                        "半装状态但找不到已解压的安装包目录；请清理 "
                        + p + " 与 /root/s5a3-bundle-* 后重试")
                self._log(f"      沿用已解压的包：{bd}", "gold")
                self._bundle_resume(cli, cfg, bd)
                self._finish_bundle_check(cli, cfg, p, compose, adv)
                return
            _, out = cli.capture("df -Pk / | awk '" + "NR==2{print $4}" + "'")
            freekb = int(out.strip()) if (out or "").strip().isdigit() else 0
            needkb = int(size / 1024 * 3)
            if freekb and freekb < needkb:
                raise RuntimeError(f"磁盘不足：可用 {freekb // 1024} MB，"
                                   f"解压+镜像约需 {needkb // 1024} MB")
            self._log(f"      磁盘可用 {freekb // 1024} MB", "ok")

            self._log(f"[2/8] 上传安装包（{human_size(size)}）", "gold")
            cli.sftp_put(local, remote_tar, self._prog_cb("上传", 5))
            lsha = sha256_file(local, self._prog_cb("校验", step=25))
            _, rsha = cli.capture(f"sha256sum {q(remote_tar)} | {AWK_FIRST}", timeout=600)
            if (rsha or "").strip() != lsha:
                raise RuntimeError("安装包上传校验失败，已中止")
            self._log("      上传校验通过（SHA256）", "ok")

            self._log("[3/8] 解压 + 包内文件校验", "gold")
            script = f"""
set -e
rm -rf {q(remote_dir)}
mkdir -p {q(remote_dir)}
tar -xzf {q(remote_tar)} -C {q(remote_dir)}
CNT=$(find {q(remote_dir)} -mindepth 1 -maxdepth 1 | wc -l)
if [ "$CNT" -eq 1 ] && [ -d "$(find {q(remote_dir)} -mindepth 1 -maxdepth 1)" ]; then
  D=$(find {q(remote_dir)} -mindepth 1 -maxdepth 1)
else
  D={q(remote_dir)}
fi
echo "BUNDLE=$D"
[ -f "$D/install.sh" ] || {{ echo "[错误] 包里没有 install.sh"; exit 1; }}
if [ -f "$D/SHA256SUMS.txt" ]; then
  cd "$D"
  if sha256sum -c SHA256SUMS.txt > /tmp/s5sums.log 2>&1; then
    echo "      包内 19 个文件校验全部 OK"
  else
    echo "[错误] SHA256SUMS 校验失败："
    tail -8 /tmp/s5sums.log
    exit 1
  fi
fi
"""
            rc, out = cli.run(script, self._line_cb, timeout=1800, collect=True)
            if rc != 0:
                raise RuntimeError("解压或包内校验失败")
            bundle_dir = ""
            for line in (out or "").splitlines():
                if line.startswith("BUNDLE="):
                    bundle_dir = line.split("=", 1)[1].strip()
            if not bundle_dir:
                raise RuntimeError("没能定位解压目录")

            self._log("[4/8] 执行 install.sh（首次 3-5 分钟）", "gold")
            hot = f"""
cd {q(bundle_dir)}
if grep -q '^set -euo pipefail' install.sh; then
  cp -f install.sh install.sh.orig
  sed -i '0,/^set -euo pipefail$/s//set -euo pipefail\\nset +o pipefail  # 热修：grep 零匹配不得被 pipefail 误判/' install.sh
  echo "      已热修 install.sh：禁用 pipefail（原文件留 install.sh.orig）"
fi
"""
            cli.run(hot, self._line_cb, timeout=120)
            rc = cli.run(f"cd {q(bundle_dir)} && COMPOSE_PROGRESS=plain bash install.sh {q(adv)} {q(p)}",
                         self._line_cb, timeout=5400)
            if rc != 0:
                self._log(f"[警告] install.sh 退出码 {rc}（包内脚本 bug："
                          f"第 6 步 grep 零匹配被 pipefail 判失败），检查现场后续做", "err")
                self._bundle_resume(cli, cfg, bundle_dir)

            self._finish_bundle_check(cli, cfg, p, compose, adv)

            self._log("[8/8] 清理临时文件", "gold")
            cli.capture(f"rm -rf {q(remote_tar)} {q(remote_dir)}", timeout=300)
            self._log(f"== 离线部署完成：{adv}:7001 ==", "ok")
        finally:
            cli.close()

    def do_pvf(self, cfg):
        local = (cfg.get("pvf_replace") or "").strip()
        if not local or not os.path.isfile(local):
            raise RuntimeError("请先选择「替换 PVF」文件")
        remote = self._rp(cfg, PVF_REL)
        self._log(f"== 替换 PVF（{human_size(os.path.getsize(local))}）==", "gold")
        cli = self._ssh(cfg)
        try:
            self._mkdir(cli, posixpath.dirname(remote))

            self._log("[1/5] 备份服务端旧 PVF", "gold")
            bk = self._backup_pvf(cli, cfg)
            if bk:
                self._download(cli, bk, cfg, "pvf")
            else:
                self._log("      没有旧文件，跳过本机备份")

            self._log("[2/5] 计算本机 SHA256", "gold")
            lsha = sha256_file(local, self._prog_cb("校验", step=25))
            self._log(f"      sha256={lsha}")

            self._log("[3/5] 上传 + 校验", "gold")
            self._put_verified(cli, local, remote, "替换 PVF", local_sha=lsha)

            self._log("[4/5] 重启游戏服", "gold")
            cli.run(f"cd {q(cfg['path'])} && docker compose -p servers5a3 "
                    f"-f {COMPOSE} up -d --force-recreate", self._line_cb, timeout=900)

            self._log("[5/5] 等待服务就绪（PVF 加载 1-3 分钟）", "gold")
            self._wait_healthy(cli, cfg)
            self._log("== PVF 已替换 ==", "ok")
        finally:
            cli.close()

    # ---------------- 数据库：备份 / 初始 / 替换 / 恢复 ----------------
    def _db_dump(self, cli, cfg):
        """导出当前库到服务器 backups/db，返回远端路径（失败返回空）。"""
        dbenv = self._rp(cfg, DBENV_REL)
        bkdir = self._rp(cfg, "backups/db")
        script = f"""
set -e
set -o pipefail 2>/dev/null || true
PW=$(grep '^MARIADB_ROOT_PASSWORD=' {q(dbenv)} | cut -d= -f2-)
[ -n "$PW" ] || {{ echo "[错误] database.env 里没有 MARIADB_ROOT_PASSWORD"; exit 1; }}
mkdir -p {q(bkdir)}
STAMP=$(date +%Y%m%d-%H%M%S)
DUMP=$(docker exec {DB_C} sh -c 'command -v mariadb-dump || command -v mysqldump' | tr -d '\\r')
[ -n "$DUMP" ] || {{ echo "[错误] 容器内找不到 mariadb-dump / mysqldump"; exit 1; }}
echo "      导出当前 {DB_NAME} …"
docker exec -e MYSQL_PWD="$PW" {DB_C} "$DUMP" -uroot --single-transaction --routines --triggers --events --databases {DB_NAME} | gzip -c > {q(bkdir)}/{DB_NAME}.$STAMP.sql.gz
[ -s {q(bkdir)}/{DB_NAME}.$STAMP.sql.gz ] || {{ echo "[错误] 备份文件为空"; exit 1; }}
echo {q(bkdir)}/{DB_NAME}.$STAMP.sql.gz
"""
        rc, out = cli.run(script, self._line_cb, timeout=3600, collect=True)
        if rc != 0:
            return ""
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if line.startswith(bkdir) and line.endswith(".sql.gz"):
                return line
        return ""

    def _db_run(self, cfg, local, backup_first, title):
        """上传 → 校验 →（可选备份）→ 停服 → 导入 → 启动。"""
        if not local or not os.path.isfile(local):
            raise RuntimeError("请先选择 SQL / SQL.gz 文件")
        gz = local.lower().endswith(".gz")
        dbenv = self._rp(cfg, DBENV_REL)
        self._log(f"== {title}：{os.path.basename(local)}"
                  f"（{human_size(os.path.getsize(local))}）==", "gold")
        cli = self._ssh(cfg)
        try:
            remote = "/tmp/s5a3_import.sql.gz" if gz else "/tmp/s5a3_import.sql"
            self._log("[1/6] 上传", "gold")
            cli.sftp_put(local, remote, self._prog_cb("上传"))
            self._log("[2/6] 校验上传（SHA256）", "gold")
            lsha = sha256_file(local, self._prog_cb("校验", step=25))
            _, rsha = cli.capture(f"sha256sum {q(remote)} | {AWK_FIRST}")
            if (rsha or "").strip() != lsha:
                raise RuntimeError("上传校验失败，已中止（数据库未改动）")
            self._log("      校验通过", "ok")

            self._log("[3/6] 检查数据库容器", "gold")
            script_a = f"""
set -e
docker inspect -f '{{{{.State.Running}}}}' {DB_C} 2>/dev/null | grep -q true || {{ echo "[错误] 数据库容器 {DB_C} 未运行"; exit 1; }}
PW=$(grep '^MARIADB_ROOT_PASSWORD=' {q(dbenv)} | cut -d= -f2-)
[ -n "$PW" ] || {{ echo "[错误] database.env 里没有 MARIADB_ROOT_PASSWORD"; exit 1; }}
if [ "{'1' if gz else '0'}" = "1" ]; then
  gunzip -t {q(remote)} || {{ echo "[错误] gz 包损坏，已中止"; exit 1; }}
  echo "      gz 完整性校验通过"
fi
"""
            rc = cli.run(script_a, self._line_cb, timeout=600)
            if rc != 0:
                raise RuntimeError("数据库检查未通过，已中止（未改动）")

            if backup_first:
                self._log("[4/6] 备份现有库", "gold")
                bk = self._db_dump(cli, cfg)
                if not bk:
                    raise RuntimeError("备份现有数据库失败，已中止（未停服、未导入）")
                self._download(cli, bk, cfg, "db")
            else:
                self._log("[4/6] 跳过备份（初始导入）", "gold")

            self._log("[5/6] 停服 + 导入", "gold")
            stop = f"""
set -e
if docker inspect {SERVER_C} >/dev/null 2>&1; then
  docker stop {SERVER_C} || {{ echo "[错误] 停止游戏服失败"; exit 1; }}
else
  echo "      游戏服容器不存在，跳过停止"
fi
"""
            rc = cli.run(stop, self._line_cb, timeout=300)
            if rc != 0:
                raise RuntimeError("停止游戏服失败，已中止（数据库未改动）")

            feed = f"gunzip -c {q(remote)}" if gz else f"cat {q(remote)}"
            script_b = f"""
set -e
set -o pipefail 2>/dev/null || true
PW=$(grep '^MARIADB_ROOT_PASSWORD=' {q(dbenv)} | cut -d= -f2-)
CLI=$(docker exec {DB_C} sh -c 'command -v mariadb || command -v mysql' | tr -d '\\r')
[ -n "$CLI" ] || {{ echo "[错误] 容器内找不到 mariadb/mysql 客户端"; exit 1; }}
echo "      {DB_NAME} 导入中（大库需要几分钟）…"
{feed} | docker exec -i -e MYSQL_PWD="$PW" {DB_C} "$CLI" -uroot || {{ echo "[错误] 导入失败"; exit 1; }}
echo "导入完成"
"""
            rc = cli.run(script_b, self._line_cb, timeout=3600)
            if rc != 0:
                raise RuntimeError("导入失败，游戏服仍处于停止状态；可用备份恢复后再启动")

            self._log("[6/6] 启动游戏服", "gold")
            rc = cli.run(f"docker start {SERVER_C}", self._line_cb, timeout=300)
            if rc != 0:
                raise RuntimeError(f"启动游戏服失败，请手动 docker start {SERVER_C}")
            self._wait_healthy(cli, cfg)
            self._log(f"== {title}完成 ==", "ok")
        finally:
            cli.close()

    def _db_backup(self, cfg):
        self._log("== 备份数据库 ==", "gold")
        cli = self._ssh(cfg)
        try:
            bk = self._db_dump(cli, cfg)
            if not bk:
                raise RuntimeError("备份失败，见上方日志")
            self._download(cli, bk, cfg, "db")
            self._log("== 数据库备份完成 ==", "ok")
        finally:
            cli.close()

    def _ask_db_file(self, title, start_dir=None):
        return filedialog.askopenfilename(
            title=title, parent=self.root,
            initialdir=start_dir if start_dir and os.path.isdir(start_dir) else None,
            filetypes=[("SQL 压缩包", "*.gz"), ("SQL", "*.sql"), ("All", "*.*")])

    def _db_import_initial(self):
        local = self._ask_db_file("选择初始数据库（新服首次导入）")
        if not local:
            return
        cfg = self._read_form()
        self._start(lambda c: self._db_run(c, local, False, "导入初始库"),
                    danger=("导入初始库",
                            [f"将直接导入：{os.path.basename(local)}",
                             "不先备份现有数据；导入期间游戏服会停止。"],
                            f"{cfg['path']} / 库 {DB_NAME}"))

    def _db_replace(self):
        local = self._ask_db_file("选择要替换导入的数据库")
        if not local:
            return
        cfg = self._read_form()
        self._start(lambda c: self._db_run(c, local, True, "替换数据库"),
                    danger=("替换数据库",
                            [f"将用 {os.path.basename(local)} 覆盖现有数据。",
                             "先整库备份（服务器+本机），再停服导入。"],
                            f"{cfg['path']} / 库 {DB_NAME}"))

    def _db_restore(self):
        cfg = self._read_form()
        d = os.path.join(self._backup_root(cfg), "db")
        local = self._ask_db_file("选择要恢复的备份", d)
        if not local:
            return
        self._start(lambda c: self._db_run(c, local, True, "恢复备份"),
                    danger=("恢复备份",
                            [f"将从备份恢复：{os.path.basename(local)}",
                             "恢复前会先备份当前库（留退路），期间游戏服停止。"],
                            f"{cfg['path']} / 库 {DB_NAME}"))

    def do_status(self, cfg):
        p = cfg["path"]
        cur = self._rp(cfg, PVF_REL)
        self._log("== 服务状态 ==", "gold")
        cli = self._ssh(cfg)
        try:
            script = f"""
cd {q(p)} 2>/dev/null || {{ echo "[错误] 目录不存在：{p}"; exit 1; }}
echo '--- 容器 ---'
docker compose -p servers5a3 -f {COMPOSE} ps 2>&1
echo '--- 健康 ---'
docker inspect -f '{{{{.Name}}}} {{{{.State.Status}}}} {{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}no-healthcheck{{{{end}}}}' {SERVER_C} {DB_C} 2>/dev/null || echo '容器不存在'
echo '--- 账号 API ---'
curl -s -m 5 http://127.0.0.1:17002/api/status || echo '(无响应)'
echo
echo '--- 端口 ---'
ss -tlnp 2>/dev/null | grep -E '7001|17002|23306' || echo '(无监听)'
echo '--- 资源 ---'
free -m | head -2
df -h / | tail -1
echo '--- PVF ---'
ls -lh {q(cur)} 2>/dev/null && sha256sum {q(cur)} | cut -c1-16 || echo 'PVF 缺失'
echo '--- 备份（服务器）---'
if [ -d {q(self._rp(cfg, 'backups/pvf'))} ]; then ls -1t {q(self._rp(cfg, 'backups/pvf'))} | head -3; else echo '暂无 PVF 备份'; fi
if [ -d {q(self._rp(cfg, 'backups/db'))} ]; then ls -1t {q(self._rp(cfg, 'backups/db'))} | head -3; else echo '暂无数据库备份'; fi
echo '--- 补丁目录 ---'
if [ -d {q(cfg['patch_dir'])} ]; then ls -1 {q(cfg['patch_dir'])}; else echo '补丁目录不存在'; fi
"""
            cli.run(script, self._line_cb, timeout=90)
        finally:
            cli.close()

    # ---------------- 通讯文件验证 ----------------
    def do_verify(self, cfg):
        """只读：核对跨端必须一致的几份文件，不改任何东西。"""
        self._log("== 通讯文件验证（只读） ==", "gold")
        cli = self._ssh(cfg)
        p = cfg["path"]
        rows = []

        def rec(ok, name, detail):
            rows.append((ok, name))
            if ok is True:
                self._log(f"[通过] {name}：{detail}", "ok")
            elif ok is False:
                self._log(f"[不一致] {name}：{detail}", "err")
            else:
                self._log(f"[跳过] {name}：{detail}", "gold")

        try:
            self._log("[1/5] PVF 三处哈希", "gold")
            sv_pvf = posixpath.join(p, PVF_REL)
            sv_sha = self._remote_sha(cli, sv_pvf)
            gm_sha = ""
            prof = {}
            prel, prof = self._gm_profile(cli)
            for k in ("PvfSha256", "pvfSha256", "pvf_sha256"):
                if prof.get(k):
                    gm_sha = str(prof[k]).lower()
                    break
            if prel:
                self._log(f"      profile：{prel}")
            cli_pvf = self._local_client_file(cfg, "Script.pvf")
            cli_sha = sha256_file(cli_pvf) if cli_pvf else ""
            if not sv_sha:
                rec(None, "PVF", f"服务端没有 {PVF_REL}")
            else:
                self._log(f"      服务端 {sv_sha[:16]}…")
                if gm_sha:
                    rec(gm_sha == sv_sha, "PVF：服务端 ↔ GM 索引", f"面板 {gm_sha[:16]}…")
                else:
                    rec(None, "PVF：服务端 ↔ GM 索引",
                        "面板 profile.json 读不到或没有 PvfSha256（试过 data/、inputs/、data/private/）")
                if cli_sha:
                    rec(cli_sha == sv_sha, "PVF：服务端 ↔ 客户端", f"客户端 {cli_sha[:16]}…")
                else:
                    rec(None, "PVF：服务端 ↔ 客户端", "未选客户端目录或里面没有 Script.pvf")

            self._log("[2/5] 频道表", "gold")
            tbl_rel = "runtime/docker/data/channel.override.etc"
            sv_tbl = self._remote_sha(cli, posixpath.join(p, tbl_rel))
            local_tbl = (cfg.get("channel_table") or "").strip()
            if not (local_tbl and os.path.isfile(local_tbl)):
                local_tbl = self._local_client_file(cfg, "assets", "channel_info_120.etc") or ""
            if not sv_tbl:
                rec(None, "频道表", f"服务端没有 {tbl_rel}（可能用的是 PVF 内置表）")
            else:
                self._log(f"      服务端 {sv_tbl[:16]}…")
                rec(sv_tbl == CHANNEL_SHA_120, "频道表 ↔ 随包定版（120 条）",
                    f"服务端 {sv_tbl[:16]}… / 定版 {CHANNEL_SHA_120[:16]}…")
                if local_tbl:
                    rec(sha256_file(local_tbl) == sv_tbl, "频道表：服务端 ↔ 本机",
                        f"{os.path.basename(local_tbl)}")
                else:
                    rec(None, "频道表：服务端 ↔ 本机", "未选频道表文件，客户端目录里也没有")

            self._log("[3/5] 端口发布 ↔ 实际监听", "gold")
            comp_txt = self._remote_text(cli, posixpath.join(p, "runtime/docker/compose.json"))
            try:
                comp = json.loads(comp_txt)
            except Exception:
                comp = None
            ports, udp_ports = [], []

            def add_spec(spec):
                """兼容 compose 短语法 "0.0.0.0:7001:7001/tcp" 与端口区间。"""
                s = str(spec).strip()
                proto = "tcp"
                if "/" in s:
                    s, proto = s.rsplit("/", 1)
                    proto = proto.lower()
                parts = s.split(":")
                host = parts[-2] if len(parts) >= 2 else parts[0]
                try:
                    if "-" in host:
                        a, b = host.split("-", 1)
                        a, b = int(a), int(b)
                        if 0 < b - a <= 2000:
                            (udp_ports if proto == "udp" else ports).extend(range(a, b + 1))
                    else:
                        (udp_ports if proto == "udp" else ports).append(int(host))
                except Exception:
                    pass

            def walk(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == "ports" and isinstance(v, list):
                            for it in v:
                                if isinstance(it, str):
                                    add_spec(it)
                                elif isinstance(it, dict) and it.get("published"):
                                    add_spec(f"{it['published']}/{it.get('protocol', 'tcp')}")
                        elif k in ("published", "host_port"):
                            add_spec(f"{v}/{o.get('protocol', 'tcp')}")
                        else:
                            walk(v)
                elif isinstance(o, list):
                    for it in o:
                        walk(it)

            walk(comp) if comp else None
            game_ports = sorted({x for x in ports if GAME_PORT_LO <= x < GAME_PORT_HI})
            if not comp:
                rec(None, "compose 端口发布", "compose.json 读取失败")
            else:
                need = [7001, 17002]
                miss = [x for x in need if x not in ports]
                if miss:
                    rec(False, "compose 必需端口发布", f"缺少 {miss}")
                else:
                    rec(True, "compose 必需端口发布", "7001 / 17002 已发布")
                if sv_tbl == CHANNEL_SHA_120:
                    rec(len(game_ports) == CHANNEL_COUNT_120, "游戏端口数 ↔ 频道条数",
                        f"已发布 {len(game_ports)} 个（10000+），定版表 {CHANNEL_COUNT_120} 条")
                else:
                    rec(None, "游戏端口数 ↔ 频道条数",
                        f"已发布 {len(game_ports)} 个游戏端口；频道表非定版，无法判定应有的条数")
            _, ss_txt = cli.capture("ss -tln 2>/dev/null")
            listen = set(int(m) for m in re.findall(r":(\d{4,5})\s", ss_txt))
            if listen:
                pub_not_listen = [x for x in game_ports if x not in listen]
                listen_not_pub = sorted(x for x in listen
                                        if GAME_PORT_LO <= x < GAME_PORT_HI and x not in game_ports)
                rec(not pub_not_listen, "已发布但未监听",
                    f"{len(pub_not_listen)} 个：{pub_not_listen[:8]}" if pub_not_listen else "无")
                rec(not listen_not_pub, "已监听但未发布",
                    f"{len(listen_not_pub)} 个：{listen_not_pub[:8]}" if listen_not_pub else "无")

            self._log("[4/5] 宣告地址", "gold")
            adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
            # 真实部署里它在 runtime/docker/server/configs 下；两个位置都试
            toml, toml_rel = "", ""
            for rel in ("runtime/docker/server/configs/dnfbridge.toml",
                        "runtime/server/configs/dnfbridge.toml"):
                toml = self._remote_text(cli, posixpath.join(p, rel))
                if toml:
                    toml_rel = rel
                    break
            mt = re.search(r'public_addr\s*=\s*"([^"]*)"', toml)
            if mt:
                rec(mt.group(1) == f"{adv}:7001", "宣告地址 ↔ dnfbridge.public_addr",
                    f"{mt.group(1)} / 期望 {adv}:7001（{toml_rel}）")
            else:
                rec(None, "宣告地址 ↔ dnfbridge.public_addr",
                    "两个常见位置都读不到 dnfbridge.toml 的 public_addr")

            self._log("[5/5] GM 密钥", "gold")
            senv = self._remote_text(cli, posixpath.join(p, "runtime/docker/server.env"))
            secret = ""
            for line in senv.splitlines():
                if line.strip().startswith("DNFBRIDGE_PLAYER_ADMIN_SECRET="):
                    secret = line.split("=", 1)[1].strip()
            if not secret:
                rec(None, "GM 密钥", "server.env 里没有 DNFBRIDGE_PLAYER_ADMIN_SECRET")
            elif len(secret) < 32:
                rec(False, "GM 密钥长度", f"{len(secret)} 位，接口会全 404（要求 ≥32）")
            else:
                rec(True, "GM 密钥长度", f"{len(secret)} 位（≥32）")
                svc = self._remote_text(cli, f"{GM_BASE}/data/service.json")
                set_txt = self._remote_text(cli, f"{GM_BASE}/settings.json")
                blob = (svc or "") + (set_txt or "")
                if not blob:
                    rec(None, "GM 密钥：服务端 ↔ 面板", "/opt/purplemagic 未安装或配置不可读")
                else:
                    rec(secret in blob, "GM 密钥：服务端 ↔ 面板",
                        f"密钥（{secret[:4]}…{len(secret)} 位）在面板配置中"
                        + ("能找到" if secret in blob else "找不到"))

            bad = [n for ok, n in rows if ok is False]
            unk = [n for ok, n in rows if ok is None]
            passed = len(rows) - len(bad) - len(unk)
            self._log(f"== 验证结束：{passed} 项通过 / {len(bad)} 项不一致 / {len(unk)} 项无法验证 ==",
                      "ok" if not bad else "err")
            if bad:
                raise RuntimeError("通讯文件不一致：" + "、".join(bad))
        finally:
            cli.close()

    # ---------------- GM 网关 ----------------
    # ---------------- GM 网关 ----------------
    def _pm_compose(self, cfg):
        return ("cd %s && docker compose --env-file %s/compose.env "
                "-f deploy/selfhost/compose.yaml" % (q(cfg["pm_src"]), q(cfg["pm_data"])))

    def _open_panel(self):
        cfg = self._read_form()
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        if not adv:
            messagebox.showwarning("缺少配置", "请先填写服务器 IP", parent=self.root)
            return
        url = "https://%s/console/" % adv
        self._log("== 打开 GM 后台 %s ==" % url, "gold")
        import webbrowser
        webbrowser.open(url)

    def do_gm_status(self, cfg):
        self._log("== GM 后台状态（compose） ==", "gold")
        cli = self._ssh(cfg)
        try:
            script = f"""
echo '--- 容器 ---'
cd {q(cfg['pm_src'])} && docker compose --env-file {q(cfg['pm_data'])}/compose.env -f deploy/selfhost/compose.yaml ps --format 'table {{{{.Name}}}}\\t{{{{.Status}}}}' 2>/dev/null || echo '      compose 未就绪'
echo '--- 端口 ---'
ss -tlnp 2>/dev/null | grep -E ':443 |:8443|:18080|:18081|:18444' || echo '      (无监听)'
echo '--- 探测 ---'
curl -sk -m 8 -o /dev/null -w '      console https://127.0.0.1/console/ : %{{http_code}}\\n' https://127.0.0.1/console/ || echo '      console 无响应'
curl -sk -m 8 -o /dev/null -w '      gateway 8443/api/status : %{{http_code}}\\n' https://127.0.0.1:8443/api/status || echo '      gateway 无响应'
curl -sk -m 8 -o /dev/null -w '      bootstrap : %{{http_code}}\\n' https://127.0.0.1:8443/launcher/v2/bootstrap.json || true
echo '--- 数据 ---'
ls -lh {q(cfg['pm_data'])}/admin-token.txt {q(cfg['pm_data'])}/indexes/items.json 2>/dev/null || echo '      缺 Token 或物品索引'
echo '--- 旧服务 ---'
for u in purplemagic-gateway purplemagic-gm nginx dnf90gm; do
  printf '      %-22s %s\\n' "$u" "$(systemctl is-active $u 2>/dev/null || true)"
done
"""
            cli.run(script, self._line_cb, timeout=180)
        finally:
            cli.close()

    def _pm_deploy_confirm(self):
        self._start(self.do_pm_deploy, danger=(
            "部署 GM 后台（Docker Compose）",
            ["会重新构建 purplemagic:local 镜像，并重启 gateway / gm / edge。",
             "缺物品索引时会自动跑 4 个索引（约 15 分钟），已存在则跳过。",
             "玩家 API 会短暂中断（几秒到几分钟，取决于是否重建镜像）。",
             "填了「GM 仓库」时会先 git reset --hard 到最新代码（该目录未提交改动会丢）。",
             "不会改动登录器档案与已发布内容。"],
            "docker compose", False))

    def do_pm_deploy(self, cfg):
        self._log("== 部署 GM 后台（PurpleMagic） ==", "gold")
        cli = self._ssh(cfg)
        try:
            rc, out = cli.capture("test -f %s/deploy/selfhost/compose.yaml && echo ok"
                                  % q(cfg["pm_src"]))
            if "ok" not in (out or ""):
                raise RuntimeError("服务器上没有 %s/deploy/selfhost/compose.yaml" % cfg["pm_src"])
            compose = self._pm_compose(cfg)

            repo = (cfg.get("pm_repo") or "").strip()
            if repo:
                self._log("      从仓库同步源码：%s" % repo)
                sync = f"""
set -e
if [ -d {q(cfg['pm_src'])}/.git ]; then
  cd {q(cfg['pm_src'])}
  git fetch --all --prune
  git reset --hard origin/HEAD 2>/dev/null || git reset --hard origin/main 2>/dev/null || git reset --hard origin/master
  echo "      当前提交：$(git log --oneline -1)"
elif [ -d {q(cfg['pm_src'])} ] && [ -n "$(ls -A {q(cfg['pm_src'])} 2>/dev/null)" ]; then
  echo "[错误] {cfg['pm_src']} 已存在但不是 git 仓库，无法从仓库更新"
  exit 1
else
  mkdir -p "$(dirname {q(cfg['pm_src'])})"
  git clone {q(repo)} {q(cfg['pm_src'])}
  echo "      当前提交：$(cd {q(cfg['pm_src'])} && git log --oneline -1)"
fi
"""
                rc = cli.run(sync, self._line_cb, timeout=1800)
                if rc != 0:
                    raise RuntimeError("从仓库同步 GM 源码失败")

            self._log("      构建镜像（首次 5-10 分钟，之后走缓存）…")
            rc = cli.run(compose + " build", self._line_cb, timeout=3600)
            if rc != 0:
                raise RuntimeError("镜像构建失败（看上面的错误行）")

            _, has = cli.capture("test -f %s/indexes/items.json && echo yes" % q(cfg["pm_data"]))
            if "yes" in (has or ""):
                self._log("      已有物品索引，跳过索引步骤")
            else:
                self._log("      未发现索引，开始建索引（约 15 分钟）…")
                for step in ("index-pvf", "index-items", "index-quests", "index-professions", "schema"):
                    self._log("      >>> %s" % step)
                    rc = cli.run(compose + " --profile setup run --rm " + step,
                                 self._line_cb, timeout=3600)
                    if rc != 0:
                        raise RuntimeError("%s 失败" % step)

            self._log("      启动 gateway / gm / edge …")
            rc = cli.run(compose + " up -d gateway gm edge", self._line_cb, timeout=900)
            if rc != 0:
                raise RuntimeError("compose up 失败")
            self._verify_pm(cli, cfg)
            self._log("== 部署完成 ==", "ok")
        finally:
            cli.close()

    def _verify_pm(self, cli, cfg):
        rc, out = cli.capture("sleep 3; curl -sk -m 10 -o /dev/null -w '%{http_code}' "
                              "https://127.0.0.1/console/")
        ok = (out or "").strip() == "200"
        rc2, out2 = cli.capture("curl -sk -m 10 -o /dev/null -w '%{http_code}' "
                                "https://127.0.0.1:8443/api/status")
        ok2 = (out2 or "").strip() == "200"
        self._log("      console=%s  gateway=%s" % (out or "?", out2 or "?"),
                  "ok" if (ok and ok2) else "err")
        if not (ok and ok2):
            raise RuntimeError("服务起来了但探测不通过：console=%s gateway=%s" % (out, out2))
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        self._log("      后台地址：https://%s/console/" % adv, "gold")
        token = self._pm_read_token(cli, cfg)
        if token:
            self._log("      登录 Token：%s" % token, "gold")

    def _pm_read_token(self, cli, cfg):
        _, out = cli.capture("cat %s/admin-token.txt 2>/dev/null" % q(cfg["pm_data"]))
        return (out or "").strip()

    def _pm_token_confirm(self):
        self._start(self.do_pm_reset_token, danger=(
            "重置 GM 管理 Token",
            ["生成新的 48 位 Token，旧 Token 立即失效。",
             "会更新 gateway.json 里的两处 sha256 并重启 gateway。",
             "新 Token 在日志里显示一次，并自动复制到剪贴板。"],
            "gateway.json", False))

    def do_pm_reset_token(self, cfg):
        self._log("== 重置 GM 管理 Token ==", "gold")
        token = secrets.token_urlsafe(36)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cli = self._ssh(cfg)
        try:
            payload = json.dumps({"token": token, "sha256": digest})
            local = os.path.join(tempfile.gettempdir(), "pm-token.json")
            with open(local, "w", encoding="utf-8") as f:
                f.write(payload)
            cli.sftp_put(local, "/tmp/pm-token.json")

            helper = (
                "import json, os, hashlib\n"
                "d = json.load(open('/tmp/pm-token.json'))\n"
                "p = %r\n"
                "cfg = json.load(open(p))\n"
                "cfg['management']['loginKeySha256'] = d['sha256']\n"
                "cfg['launcher_hub']['admin_token_sha256'] = d['sha256']\n"
                "tmp = p + '.tmp'\n"
                "json.dump(cfg, open(tmp, 'w'), ensure_ascii=False, indent=2)\n"
                "os.chmod(tmp, 0o600)\n"
                "os.replace(tmp, p)\n"
                "open(os.path.join(os.path.dirname(p), 'admin-token.txt'), 'w').write(d['token'] + '\\n')\n"
                "os.chmod(os.path.join(os.path.dirname(p), 'admin-token.txt'), 0o600)\n"
                "print('token updated')\n"
            ) % (cfg["pm_data"] + "/gateway.json")
            with open(local, "w", encoding="utf-8", newline="\n") as f:
                f.write(helper)
            cli.sftp_put(local, "/tmp/pm-reset-token.py")

            rc, out = cli.capture("runuser -u dnf90gm -- env HOME=/tmp python3 /tmp/pm-reset-token.py 2>&1")
            self._log("      " + (out or "").strip()[:200])
            if "token updated" not in (out or ""):
                raise RuntimeError("写 gateway.json 失败")

            self._log("      重启 gateway …")
            rc = cli.run(self._pm_compose(cfg) + " restart gateway", self._line_cb, timeout=300)
            if rc != 0:
                raise RuntimeError("重启 gateway 失败")
            self._verify_pm_login(cli, cfg, token)
        finally:
            try:
                cli.capture("rm -f /tmp/pm-token.json /tmp/pm-reset-token.py")
            except Exception:
                pass
            cli.close()

    def _verify_pm_login(self, cli, cfg, token):
        rc, out = cli.capture(
            "curl -sk -m 10 -H 'Authorization: Bearer %s' -o /dev/null -w '%%{http_code}' "
            "https://127.0.0.1/admin/api/news" % token)
        code = (out or "").strip()
        ok = code == "200"
        self._log("      Token 校验 HTTP=%s%s" % (code, "" if ok else "（200 才算通过）"),
                  "ok" if ok else "err")
        try:
            self.root.after(0, self._copy_to_clipboard, token)
        except Exception:
            pass
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        self._log("      ------------------------------------------------", "gold")
        self._log("      后台：https://%s/console/" % adv, "gold")
        self._log("      新 Token（已复制到剪贴板）：", "gold")
        self._log("          %s" % token, "ok")
        self._log("      ------------------------------------------------", "gold")

    def _copy_to_clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception:
            pass

    def _pm_reindex_confirm(self):
        self._start(self.do_pm_reindex, danger=(
            "重建 PVF 与物品索引",
            ["会重跑 PVF / 物品 / 任务 / 职业四个索引。",
             "124MB PVF 约 15 分钟，期间 GM 查询会变慢或不可用。",
             "不动数据库与玩家数据。"],
            "indexes", False))

    def do_pm_reindex(self, cfg):
        self._log("== 重建索引 ==", "gold")
        cli = self._ssh(cfg)
        try:
            compose = self._pm_compose(cfg)
            for step in ("index-pvf", "index-items", "index-quests", "index-professions"):
                self._log("      >>> %s" % step)
                rc = cli.run(compose + " --profile setup run --rm " + step,
                             self._line_cb, timeout=3600)
                if rc != 0:
                    raise RuntimeError("%s 失败" % step)
            self._log("      重启 gm 让新索引生效 …")
            cli.run(compose + " restart gm", self._line_cb, timeout=300)
            self._verify_pm(cli, cfg)
            self._log("== 索引重建完成 ==", "ok")
        finally:
            cli.close()

    # ---------------- 登录器 ----------------
    # ---------------- 登录器 ----------------
    def _detect_release_dir(self, cli):
        """从网关配置里找 release_dir，找不到返回空。"""
        for p in (f"{GM_BASE}/data/gateway.json", f"{GM_BASE}/settings.json",
                  f"{GM_BASE}/data/service.json"):
            txt = self._remote_text(cli, p)
            if not txt:
                continue
            m = re.search(r'"release_dir"\s*:\s*"([^"]+)"', txt)
            if m:
                return m.group(1)
        return ""

    def _launcher_upload(self):
        cfg = self._read_form()
        exe = (cfg.get("launcher_exe") or "").strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showwarning("缺少配置", "请先在「登录器」页选择登录器 EXE",
                                   parent=self.root)
            return
        self._start(self.do_launcher_upload, confirm=(
            "上传登录器",
            f"将上传 {os.path.basename(exe)}"
            f"（{human_size(os.path.getsize(exe))}）到服务器发布目录，\n"
            "玩家从 /launcher/PurpleMagic.exe 下载。\n\n继续？"))

    def do_launcher_upload(self, cfg):
        local = (cfg.get("launcher_exe") or "").strip()
        if not local or not os.path.isfile(local):
            raise RuntimeError("未选择登录器 EXE")
        self._log(f"== 上传登录器（{human_size(os.path.getsize(local))}）==", "gold")
        cli = self._ssh(cfg)
        try:
            rel = (cfg.get("release_dir") or "").strip()
            if not rel:
                rel = self._detect_release_dir(cli)
                if rel:
                    self._log(f"      从网关配置探测到 release_dir：{rel}")
            if not rel:
                rel = posixpath.join(GM_BASE, "data/release")
                self._log(f"[警告] 没探测到 release_dir，改用默认 {rel}", "err")
            name = "PurpleMagic.exe"
            self._mkdir(cli, rel)
            dst = posixpath.join(rel, name)
            self._put_verified(cli, local, dst, "登录器")
            adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
            _, code = cli.capture(
                f"curl -s -o /dev/null -w '%{{http_code}}' -m 8 "
                f"http://127.0.0.1/launcher/{name}")
            code = (code or "").strip()
            if code == "200":
                self._log(f"      下载路径可用：http://{adv}/launcher/{name}", "ok")
            else:
                self._log(f"[警告] /launcher/{name} 返回 {code or '无响应'}", "err")
                self._log("       确认网关已重启并加载新发布目录，或检查 nginx 反代", "err")
            self._log("== 登录器上传完成 ==", "ok")
        finally:
            cli.close()

    # ---------------- 通讯文件（签名 profile） ----------------
    # 登录器只认服务端签名发布的 bootstrap.json；里面写死 ExeSha256 / PvfSha256。
    # 「生成通讯文件」= 用本机客户端重算这两个哈希，重建 profile，sequence+1，
    # 写回 inputs / private 并在 hub state 里发布，再重启网关；「检查通讯文件」只读核对 + 验签。
    PROFILE_INPUTS = GM_BASE + "/inputs/profile.json"
    PROFILE_PRIVATE = GM_BASE + "/data/private/player-profile.json"
    PROFILE_STATE = GM_BASE + "/data/hub/state.json"
    PROFILE_CERT = GM_BASE + "/data/tls-player/server.crt"

    def _bootstrap(self, cli):
        """读登录器实际拿到的签名档案，返回解析后的 payload（含原始载荷与签名）。"""
        rc, out = cli.capture(
            "curl -sk --http1.1 -m 12 https://127.0.0.1:8443/launcher/v2/bootstrap.json")
        out = (out or "").strip()
        if rc != 0 or not out.startswith("{"):
            return None
        try:
            doc = json.loads(out)
            raw = base64.b64decode(doc.get("Payload", ""))
            payload = json.loads(raw)
            payload["_payload"] = raw
            payload["_signature"] = base64.b64decode(doc.get("Signature", ""))
            return payload
        except Exception:
            return None

    def _local_client_sha(self, cfg):
        d = (cfg.get("client_dir") or "").strip()
        out = {}
        for name in ("DNF.exe", "Script.pvf"):
            p = os.path.join(d, name) if d else ""
            out[name] = sha256_file(p) if p and os.path.isfile(p) else ""
        return out

    def _verify_profile_signature(self, cli, boot, prof):
        """用 profile 里的 UpdatePublicKey 验 bootstrap 的 RSA-PSS 签名。"""
        pub = (prof or {}).get("UpdatePublicKey") or ""
        sig = boot.get("_signature") or b""
        raw = boot.get("_payload") or b""
        if not pub or not sig:
            self._log("[跳过] 签名：profile 没有公钥或 bootstrap 没有签名", "gold")
            return None
        tmp = "/tmp/dsh-profile-check"
        cli.capture("rm -rf %s && mkdir -p %s" % (q(tmp), q(tmp)))
        local_tmp = tempfile.mkdtemp(prefix="dsh-prof-")
        try:
            for name, data in (("payload.bin", raw), ("sig.bin", sig),
                               ("pub.pem", pub.encode("utf-8"))):
                lp = os.path.join(local_tmp, name)
                with open(lp, "wb") as f:
                    f.write(data)
                cli.sftp_put(lp, tmp + "/" + name)
        finally:
            shutil.rmtree(local_tmp, ignore_errors=True)
        rc, out = cli.capture(
            "openssl dgst -sha256 -sigopt rsa_padding_mode:pss -sigopt rsa_pss_saltlen:32 "
            "-verify %s/pub.pem -signature %s/sig.bin %s/payload.bin"
            % (q(tmp), q(tmp), q(tmp)))
        ok = "Verified OK" in (out or "")
        self._log(("[通过] " if ok else "[不一致] ") + "bootstrap 签名（RSA-PSS）："
                  + ("Verified OK" if ok else ((out or "验签失败").strip()[:120])),
                  "ok" if ok else "err")
        return ok

    def do_profile_check(self, cfg):
        self._log("== 通讯文件检查（只读） ==", "gold")
        cli = self._ssh(cfg)
        rows = []
        try:
            boot = self._bootstrap(cli)
            if not boot:
                raise RuntimeError("读不到 /launcher/v2/bootstrap.json（网关没起，或不是这套部署）")
            prof = boot.get("profile") or {}
            self._log("      sequence  : %s" % boot.get("sequence"))
            for k in ("ExeSha256", "PvfSha256", "GameHost", "BootstrapPort",
                      "ChannelServerId", "MinimumLauncherVersion"):
                if prof.get(k) not in (None, ""):
                    self._log("      %-22s %s" % (k + ":", prof.get(k)))
            local = self._local_client_sha(cfg)
            if local["DNF.exe"]:
                same = local["DNF.exe"].lower() == str(prof.get("ExeSha256", "")).lower()
                rows.append(same)
                self._log(("[通过] " if same else "[不一致] ") + "本机 DNF.exe ↔ 档案：本机 %s… / 档案 %s…"
                          % (local["DNF.exe"][:16], str(prof.get("ExeSha256", ""))[:16]),
                          "ok" if same else "err")
            else:
                self._log("[跳过] 本机 DNF.exe：未选客户端目录或文件不存在", "gold")
            if local["Script.pvf"]:
                same = local["Script.pvf"].lower() == str(prof.get("PvfSha256", "")).lower()
                rows.append(same)
                self._log(("[通过] " if same else "[不一致] ") + "本机 Script.pvf ↔ 档案：本机 %s… / 档案 %s…"
                          % (local["Script.pvf"][:16], str(prof.get("PvfSha256", ""))[:16]),
                          "ok" if same else "err")
            else:
                self._log("[跳过] 本机 Script.pvf：未选客户端目录或文件不存在", "gold")
            sig_ok = self._verify_profile_signature(cli, boot, prof)
            if sig_ok is False:
                rows.append(False)
            bad = [r for r in rows if not r]
            self._log("== 检查结束：%d 项通过 / %d 项不一致 ==" % (len(rows) - len(bad), len(bad)),
                      "ok" if not bad else "err")
            if bad:
                raise RuntimeError("通讯文件与本地客户端不一致，请核对后再发布")
        finally:
            cli.close()

    def _profile_generate_confirm(self):
        cfg = self._read_form()
        local = self._local_client_sha(cfg)
        if not local["DNF.exe"] or not local["Script.pvf"]:
            messagebox.showwarning(
                "缺少客户端", "请先在「客户端目录」选含 DNF.exe 与 Script.pvf 的目录",
                parent=self.root)
            return
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        self._start(self.do_profile_generate, danger=(
            "生成并发布通讯文件",
            ["用本机客户端重算 ExeSha256 / PvfSha256，重建 profile 并发布新序列。",
             "sequence 会 +1，登录器下次启动按新档案校验客户端。",
             "不在新档案里的客户端会被拒绝登录。",
             "目标：https://%s:8443（网关会重启）" % adv],
            GM_BASE + "/data/hub/state.json"))

    def do_profile_generate(self, cfg):
        self._log("== 生成通讯文件（写服务器） ==", "gold")
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        local = self._local_client_sha(cfg)
        if not local["DNF.exe"] or not local["Script.pvf"]:
            raise RuntimeError("客户端目录里缺 DNF.exe 或 Script.pvf")
        self._log("      本机 DNF.exe  %s…" % local["DNF.exe"][:16])
        self._log("      本机 Script.pvf %s…" % local["Script.pvf"][:16])
        cli = self._ssh(cfg)
        try:
            # 1) 以服务端现有 profile 为模板（保留 ChannelServerId / ChannelHistory 等字段）
            rc, txt = cli.capture("cat %s" % q(self.PROFILE_INPUTS))
            prof = None
            if rc == 0 and (txt or "").strip().startswith("{"):
                try:
                    prof = json.loads(txt)
                except Exception:
                    prof = None
            if prof is None:
                rc, txt = cli.capture("cat %s" % q(self.PROFILE_PRIVATE))
                if rc != 0 or not (txt or "").strip().startswith("{"):
                    raise RuntimeError("服务端读不到现有 profile.json（GM 网关没装？）")
                prof = json.loads(txt)

            # 2) TLS 证书 DER 指纹
            rc, der = cli.capture(
                "openssl x509 -in %s -outform DER 2>/dev/null | openssl dgst -sha256"
                % q(self.PROFILE_CERT))
            m = re.search(r"([0-9a-fA-F]{64})", der or "")
            cert_sha = m.group(1).lower() if m else ""
            if cert_sha:
                self._log("      TLS 证书指纹 %s…" % cert_sha[:16])
            else:
                self._log("[警告] 读不到玩家 TLS 证书，保留 profile 里原指纹", "err")

            # 3) 服务端运行时 PVF 与本机客户端是否同一份（只提示，不改）
            rc, sv_pvf = cli.capture(
                "sha256sum %s | awk '{print $1}'" % q(posixpath.join(cfg["path"], PVF_REL)))
            sv_pvf = (sv_pvf or "").strip()
            if sv_pvf and sv_pvf != local["Script.pvf"]:
                self._log("[警告] 服务端 PVF %s… 与客户端 %s… 不是同一份"
                          % (sv_pvf[:16], local["Script.pvf"][:16]), "err")

            prof["ApiUrl"] = "https://%s:8443" % adv
            prof["SettingsUrl"] = "https://%s:8443/launcher/settings.json" % adv
            prof["GameHost"] = adv
            prof["ExeSha256"] = local["DNF.exe"]
            prof["PvfSha256"] = local["Script.pvf"]
            if cert_sha:
                prof["CertificateSha256"] = cert_sha

            # 4) hub state：published.profile 同步 + sequence+1
            rc, st = cli.capture("cat %s" % q(self.PROFILE_STATE))
            state = json.loads(st) if rc == 0 and (st or "").strip().startswith("{") else None
            if state is None:
                raise RuntimeError("读不到 hub state.json（网关没装？）")
            pub = state.get("published") or {}
            old_seq = pub.get("sequence", 1)
            pub["profile"] = prof
            pub["sequence"] = int(old_seq) + 1
            state["published"] = pub

            # 5) 落地三个文件（临时目录 → install，保持 dnf90gm 属主与权限）
            local_tmp = tempfile.mkdtemp(prefix="dsh-profgen-")
            try:
                items = [("profile-inputs.json", prof, 0o644),
                         ("player-profile.json", prof, 0o600),
                         ("state.json", state, 0o600)]
                for name, obj, mode in items:
                    lp = os.path.join(local_tmp, name)
                    with open(lp, "w", encoding="utf-8", newline="\n") as f:
                        json.dump(obj, f, ensure_ascii=False, indent=2)
                    cli.sftp_put(lp, "/tmp/" + name)
                script = (
                    "set -e\n"
                    "install -o dnf90gm -g dnf90gm -m 644 /tmp/profile-inputs.json %s\n"
                    "install -o dnf90gm -g dnf90gm -m 600 /tmp/player-profile.json %s\n"
                    "install -o dnf90gm -g dnf90gm -m 600 /tmp/state.json %s\n"
                    "rm -f /tmp/profile-inputs.json /tmp/player-profile.json /tmp/state.json\n"
                    % (q(self.PROFILE_INPUTS), q(self.PROFILE_PRIVATE), q(self.PROFILE_STATE)))
                rc = cli.run(script, self._line_cb, timeout=120)
                if rc != 0:
                    raise RuntimeError("写入 profile / state 失败")
            finally:
                shutil.rmtree(local_tmp, ignore_errors=True)

            # 6) 重启网关并读回验证
            self._log("      重启 purplemagic-gateway …", "gold")
            rc = cli.run("systemctl restart purplemagic-gateway; sleep 3; "
                         "systemctl is-active purplemagic-gateway", self._line_cb, timeout=120)
            boot = self._bootstrap(cli)
            if not boot:
                raise RuntimeError("网关重启后读不到 bootstrap.json，谨慎处理：检查 systemctl status")
            prof2 = boot.get("profile") or {}
            self._log("      新 sequence %s" % boot.get("sequence"))
            ok_exe = str(prof2.get("ExeSha256", "")).lower() == local["DNF.exe"].lower()
            ok_pvf = str(prof2.get("PvfSha256", "")).lower() == local["Script.pvf"].lower()
            self._log(("[通过] " if ok_exe else "[不一致] ") + "档案 ExeSha256 = %s…"
                      % str(prof2.get("ExeSha256", ""))[:16], "ok" if ok_exe else "err")
            self._log(("[通过] " if ok_pvf else "[不一致] ") + "档案 PvfSha256 = %s…"
                      % str(prof2.get("PvfSha256", ""))[:16], "ok" if ok_pvf else "err")
            sig_ok = self._verify_profile_signature(cli, boot, prof2)
            if not (ok_exe and ok_pvf and sig_ok is not False):
                raise RuntimeError("发布后读回校验未通过")
            self._log("== 通讯文件已发布：sequence %s → %s ==" % (old_seq, boot.get("sequence")), "ok")
        finally:
            cli.close()

    # ---------------- 客户端整包更新（pack） ----------------
    def _pack_download(self, url):
        """把直链整包下到临时文件，返回 (size, sha256, 本地路径)。"""
        import urllib.request
        tmp = os.path.join(tempfile.gettempdir(), "s5a3-pack-download.zip")
        h = hashlib.sha256()
        size = 0
        last = -1
        req = urllib.request.Request(url, headers={"User-Agent": "S5A3Tool/1.0"})
        self._log("      下载直链 …")
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
                if total:
                    pct = int(size * 100 / total)
                    if pct >= last + 10:
                        last = pct
                        self._log("      下载 %d%%（%s / %s）" % (pct, human_size(size), human_size(total)))
        return size, h.hexdigest(), tmp

    def _pack_validate_zip(self, path):
        """只允许 client/ 下的文件，或根目录的登录器；返回 (文件数, client 数, 是否含登录器)。"""
        import zipfile
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            bad, client, has_entry = [], 0, False
            for n in names:
                parts = n.replace("\\", "/").split("/")
                if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", n):
                    bad.append(n)
                    continue
                low = n.lower()
                if low.startswith("client/"):
                    client += 1
                elif "/" not in n and low.endswith(".exe"):
                    has_entry = True
                else:
                    bad.append(n)
            if bad:
                raise RuntimeError("包内路径不合规（只允许 client/ 下文件，或根目录登录器 exe）："
                                   + "、".join(bad[:5]))
            if client == 0:
                raise RuntimeError("包里没有 client/ 下的文件")
            return len(names), client, has_entry

    def _pack_publish_confirm(self):
        cfg = self._read_form()
        src = (cfg.get("pack_src") or "").strip()
        ver = (cfg.get("pack_version") or "").strip()
        if not src or not ver:
            messagebox.showwarning("缺少配置", "请填「更新包」和「版本号」", parent=self.root)
            return
        self._start(self.do_pack_publish, danger=(
            "发布客户端更新包",
            ["登录器下次启动会按新版本整包下载并覆盖客户端文件。",
             "版本：%s" % ver,
             "来源：%s" % src,
             "发布后 sequence +1；旧登录器不认识该字段，不受影响。"],
            GM_BASE + "/data/hub/state.json"))

    def do_pack_publish(self, cfg):
        self._log("== 发布客户端更新包 ==", "gold")
        src = (cfg.get("pack_src") or "").strip()
        ver = (cfg.get("pack_version") or "").strip()
        if not re.match(r"^https?://", src, re.I):
            if os.path.isfile(src):
                size = os.path.getsize(src)
                sha = sha256_file(src, self._prog_cb("校验", 10))
                n, c, entry = self._pack_validate_zip(src)
                self._log("      本地包 %s  sha256=%s…" % (human_size(size), sha[:16]))
                self._log("      zip 校验通过：%d 个文件（client/ 下 %d 个，含登录器=%s）" % (n, c, entry))
                raise RuntimeError("发布需要直链 URL：把包传到网盘/直链站后，把链接填到「更新包」")
            raise RuntimeError("更新包既不是 http/https 直链，也不是本机文件")
        size, sha, local = self._pack_download(src)
        n, c, entry = self._pack_validate_zip(local)
        self._log("      包 %s  sha256=%s…" % (human_size(size), sha[:16]))
        self._log("      zip 校验通过：%d 个文件（client/ 下 %d 个，含登录器=%s）" % (n, c, entry))
        body = {"version": ver, "url": src, "length": size, "sha256": sha,
                "notes": "发布自运维工具"}
        if entry:
            body["entry"] = "PurpleMagic.exe"
        cli = self._ssh(cfg)
        try:
            rc, token = cli.capture("cat %s/data/admin-token.txt" % GM_BASE)
            token = (token or "").strip()
            if len(token) != 48:
                raise RuntimeError("读不到网关 admin token")
            raw = base64.b64encode(json.dumps(body, ensure_ascii=False).encode("utf-8")).decode()
            cmd = ("echo %s | base64 -d | curl -sk --http1.1 -m 30 -X PUT "
                   "-H 'Authorization: Bearer %s' -H 'Content-Type: application/json' "
                   "--data-binary @- -o /tmp/pack-pub.json -w '%%{http_code}' "
                   "https://127.0.0.1:18444/admin/api/pack" % (q(raw), token))
            rc, code = cli.capture(cmd)
            code = (code or "").strip()
            self._log("      网关返回 HTTP %s" % code)
            if code != "200":
                _, err = cli.capture("cat /tmp/pack-pub.json")
                raise RuntimeError("发布失败：%s" % (err or code)[:300])
            boot = self._bootstrap(cli)
            if not boot or not boot.get("pack"):
                raise RuntimeError("发布后 bootstrap 里没有 pack")
            got = boot["pack"]
            ok = (got.get("version") == ver and str(got.get("sha256", "")).lower() == sha.lower()
                  and int(got.get("length", 0)) == size)
            self._log(("[通过] " if ok else "[不一致] ") + "读回 pack：version=%s length=%s sha=%s…"
                      % (got.get("version"), got.get("length"), str(got.get("sha256"))[:16]),
                      "ok" if ok else "err")
            sig = self._verify_profile_signature(cli, boot, boot.get("profile") or {})
            if not ok or sig is False:
                raise RuntimeError("发布后读回校验未通过")
            self._log("== 更新包已发布：sequence %s，版本 %s ==" % (boot.get("sequence"), ver), "ok")
        finally:
            cli.close()

    def do_pack_check(self, cfg):
        self._log("== 检查客户端更新包（只读） ==", "gold")
        cli = self._ssh(cfg)
        try:
            boot = self._bootstrap(cli)
            if not boot:
                raise RuntimeError("读不到 bootstrap.json")
            pack = boot.get("pack")
            self._log("      sequence : %s" % boot.get("sequence"))
            if not pack:
                self._log("[跳过] 当前没有已发布的更新包（登录器不会自动更新客户端）", "gold")
                return
            for k in ("version", "length", "sha256", "url", "entry", "notes"):
                if pack.get(k) not in (None, ""):
                    self._log("      %-8s %s" % (k + ":", pack.get(k)))
            sig = self._verify_profile_signature(cli, boot, boot.get("profile") or {})
            url = str(pack.get("url") or "")
            if re.match(r"^https?://", url, re.I):
                import urllib.request
                try:
                    req = urllib.request.Request(url, method="HEAD",
                                                 headers={"User-Agent": "S5A3Tool/1.0"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        length = int(r.headers.get("Content-Length") or 0)
                    same = length == int(pack.get("length") or 0)
                    self._log(("[通过] " if same else "[不一致] ") + "直链可达，长度 %s（包内声明 %s）"
                              % (length, pack.get("length")), "ok" if same else "err")
                except Exception as e:
                    self._log("[警告] 直链 HEAD 失败：%s（有的网盘不支持 HEAD，可忽略）" % e, "err")
            self._log("== 检查结束 ==", "ok" if sig is not False else "err")
        finally:
            cli.close()

    def do_launcher(self, cfg):
        exe = (cfg.get("launcher_exe") or "").strip()
        if not exe or not os.path.isfile(exe):
            raise RuntimeError("请先选「登录器 EXE」")
        adv = (cfg.get("advertise_ip") or cfg["host"]).strip()
        self._log("== 启动登录器 ==", "gold")
        self._log(f"      频道引导：{adv}:7001")
        self._log(f"      GM 面板  ：http://{adv}/console/")
        self._log("      连不上先跑「通讯验证」，看端口和频道表是否对得上")
        subprocess.Popen([exe], cwd=os.path.dirname(exe) or None)
        self._log(f"      已拉起：{os.path.basename(exe)}", "ok")


def main():
    if "--selftest" in sys.argv:
        root = tk.Tk()
        app = App(root)
        root.update_idletasks()
        app._log("selftest ok", "ok")
        root.update()
        root.destroy()
        print("selftest ok")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
