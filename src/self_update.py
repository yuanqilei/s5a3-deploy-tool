"""Signed GitHub updates and a separate-process, EXE-only Windows installer."""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from update_trust import PUBLIC_KEY

REPO = "yuanqilei/s5a3-deploy-tool"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"
MAX_EXE = 200 * 1024 * 1024


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}\.\d{1,6}", value):
        raise ValueError("更新版本格式无效")
    return tuple(map(int, value.split(".")))


def verify(envelope, public_key=PUBLIC_KEY):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise ValueError("更新清单格式无效")
    raw = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, raw)
    data = json.loads(raw)
    if data.get("format") != 1 or data.get("product") != "s5a3-deploy-tool":
        raise ValueError("更新产品不匹配")
    version(data.get("version"))
    asset = f"S5A3-ServerManager-{data['version']}.exe"
    expected = f"https://github.com/{REPO}/releases/download/v{data['version']}/{asset}"
    if data.get("url") != expected:
        raise ValueError("更新下载地址不匹配")
    if type(data.get("size")) is not int or not 1024 <= data["size"] <= MAX_EXE:
        raise ValueError("更新文件大小无效")
    if not re.fullmatch(r"[a-f0-9]{64}", data.get("sha256", "")):
        raise ValueError("更新摘要无效")
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 8000:
        raise ValueError("更新说明无效")
    return data


def _open(url):
    return urlopen(Request(url, headers={"User-Agent": "S5A3-Update/1", "Cache-Control": "no-cache"}), timeout=30)


def check(current):
    try:
        with _open(MANIFEST_URL) as response:
            raw = response.read(65537)
    except HTTPError as error:
        if error.code == 404:
            return None  # No signed update release has been published yet.
        raise
    if len(raw) > 65536:
        raise ValueError("更新清单过大")
    envelope = json.loads(raw)
    data = verify(envelope)
    return (envelope, data) if version(data["version"]) > version(current) else None


def validate_file(path, data):
    path = Path(path)
    if path.stat().st_size != data["size"]:
        raise ValueError("更新文件长度不符")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("更新文件不是 Windows 程序")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != data["sha256"]:
        raise ValueError("更新文件校验失败")


def stage(envelope, current):
    data = verify(envelope)
    if version(data["version"]) <= version(current):
        raise ValueError("拒绝旧版本更新")
    folder = Path(tempfile.mkdtemp(prefix="s5a3-update-"))
    exe = folder / "download.exe"
    try:
        total = 0
        deadline = time.monotonic() + 600
        with _open(data["url"]) as response, exe.open("xb") as output:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("更新下载超时")
                block = response.read(256 * 1024)
                if not block:
                    break
                total += len(block)
                if total > data["size"]:
                    raise ValueError("更新文件超过声明长度")
                output.write(block)
        validate_file(exe, data)
        return folder
    except Exception:
        exe.unlink(missing_ok=True)
        raise


def launch_helper(folder, envelope, current):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise RuntimeError("请在打包后的 Windows EXE 中安装更新")
    folder = Path(folder).resolve()
    target = Path(sys.executable).resolve()
    data = verify(envelope)
    validate_file(folder / "download.exe", data)
    job = {"target": str(target), "parent": os.getpid(), "current": current,
           "envelope": envelope, "nonce": os.urandom(24).hex()}
    job_path = folder / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    helper = folder / "installer.exe"
    shutil.copy2(target, helper)
    return subprocess.Popen([str(helper), "--apply-update", str(job_path)],
                            cwd=str(folder), creationflags=subprocess.CREATE_NO_WINDOW)


def _wait_parent(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Parent already exited.
            return
        raise OSError("无法等待主程序退出")
    try:
        if kernel.WaitForSingleObject(handle, 120000) != 0:
            raise TimeoutError("主程序未退出，取消更新")
    finally:
        kernel.CloseHandle(handle)


def acknowledge(job_path, current):
    job_path = Path(job_path).resolve()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    data = verify(job["envelope"])
    if data["version"] != current or Path(job["target"]).resolve() != Path(sys.executable).resolve():
        raise ValueError("更新启动回执不匹配")
    validate_file(sys.executable, data)
    marker = job_path.parent / "ready.tmp"
    marker.write_text(json.dumps({"nonce": job["nonce"], "pid": os.getpid(), "version": current}), encoding="utf-8")
    os.replace(marker, job_path.parent / "ready.json")


def _replace(source, target):
    # Virus scanners and onefile bootloader teardown can briefly hold the EXE.
    for attempt in range(40):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.25)


def apply(job_path):
    """Run only from a copied helper, never overwrite configuration files."""
    job_path = Path(job_path).resolve()
    folder = job_path.parent
    replaced = False
    child = None
    target = backup = None
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        data = verify(job["envelope"])
        if version(data["version"]) <= version(job["current"]):
            raise ValueError("拒绝版本回退")
        target = Path(job["target"]).resolve()
        if target.suffix.lower() != ".exe" or target == Path(sys.executable).resolve():
            raise ValueError("更新目标无效")
        staged = folder / "download.exe"
        validate_file(staged, data)
        _wait_parent(int(job["parent"]))
        backup = target.with_name(target.name + ".previous")
        incoming = target.with_name(target.name + ".incoming-" + job["nonce"])
        shutil.copy2(staged, incoming)
        validate_file(incoming, data)
        _replace(target, backup)
        replaced = True
        _replace(incoming, target)
        child = subprocess.Popen([str(target), "--update-ready", str(job_path)], cwd=str(target.parent))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            marker = folder / "ready.json"
            if marker.exists():
                ready = json.loads(marker.read_text(encoding="utf-8"))
                # PyInstaller onefile uses a bootloader parent and an app child;
                # Popen.pid is not the PID executing the Tk application.
                if ready.get("nonce") == job["nonce"] and ready.get("version") == data["version"] and type(ready.get("pid")) is int and ready["pid"] > 0:
                    (folder / "result.txt").write_text("updated " + data["version"], encoding="utf-8")
                    return 0
            if child.poll() is not None:
                break
            time.sleep(0.25)
        raise RuntimeError("新版未正常启动，正在恢复旧版")
    except Exception as error:
        detail = str(error)
        try:
            if child is not None and child.poll() is None:
                if os.name == "nt":
                    subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                                    "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=15)
                else:
                    child.terminate()
                child.wait(timeout=10)
            if replaced and backup is not None and backup.exists():
                _replace(backup, target)
                subprocess.Popen([str(target), "--update-rollback"], cwd=str(target.parent))
        except Exception as recovery:
            detail += "\n自动恢复未完成，请用 .previous 备份手动恢复：" + str(recovery)
        (folder / "result.txt").write_text(detail, encoding="utf-8")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "更新未完成，原程序已保留或恢复。\n" + detail, "S5A3 更新", 0x10)
        return 1
