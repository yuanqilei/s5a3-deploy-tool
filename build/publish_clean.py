# -*- coding: utf-8 -*-
"""生成「纯净版」发布目录：只放运行时 exe + 使用说明 + 校验值，不含任何源码/构建文件。

用法：
  python publish_clean.py --exe <exe路径> --version 2026.11.20.1 [--target <目录>]

特性：
- 目标目录先清空再写入（覆盖式发布）
- 写入后自检：目录内不得出现 .py/.spec/.ps1/.cs/.go/.git 等源码或构建物
- 生成 使用说明.txt、SHA256SUMS.txt、版本信息.txt
"""
import argparse
import hashlib
import os
import shutil
import sys

DEFAULT_TARGET = r"C:\DNF\管理工具\S5A3服务端管理工具"
EXE_NAME = "S5A3服务端管理.exe"

FORBIDDEN_EXT = {".py", ".pyc", ".spec", ".ps1", ".bat", ".cs", ".go", ".ts", ".tsx",
                 ".js", ".json", ".toml", ".yml", ".yaml", ".ico", ".jpg", ".png",
                 ".pdb", ".dll", ".zip", ".7z"}
ALLOWED_NAMES = {EXE_NAME, "使用说明.txt", "SHA256SUMS.txt", "版本信息.txt"}

README = """S5A3 服务端管理工具  v{ver}
================================

这是一个绿色版工具：解压即用，不需要安装，不含源码。

一、能做什么
------------
四个页签：
  1. 游戏服务端   —— 部署/更新游戏服源码、替换 PVF、查看状态
  2. 数据库管理   —— 连接、备份、执行 SQL
  3. 登录器服务端 —— 部署 GM 网关/后台、重建索引、重置 Token
  4. GM 工具      —— 账号/角色/邮件等常用 GM 操作

二、第一次使用
--------------
1. 双击 {exe} 打开（Windows 10/11 x64）。
2. 到「游戏服务端」页填写：服务器 IP、root 密码、远程仓库（已预填默认值）。
3. 点「检测连接」，通了再操作其它按钮。
4. 填完点右上角「保存配置」——密码会用 Windows DPAPI 加密保存在本机，
   只对这台电脑的这个用户有效，换机/重装后需重填。

三、注意
--------
- 红色按钮是危险操作（一键部署 / 更新服务端 / 替换 PVF / 重建索引 / 重置 Token），
  执行前请先确认备份。
- 首次建索引约 15 分钟，中途不要关窗口。
- 换 PVF 后必须重启游戏服并重建索引。
- 配置存在 exe 同目录的 s5a3_config.json（加密），不要把它发给别人。

四、校验
--------
文件：{exe}
大小：{size} 字节
SHA-256：{sha}

校验方法（PowerShell）：
  Get-FileHash .\\{exe} -Algorithm SHA256

五、版本
--------
当前版本：{ver}
如与本目录「版本信息.txt」不一致，说明文件被替换，请重新获取。
"""

VERSION_TXT = """版本：{ver}
构建时间：{mtime}
文件：{exe}
大小：{size} 字节
SHA-256：{sha}
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    args = ap.parse_args()

    exe = os.path.abspath(args.exe)
    if not os.path.isfile(exe):
        sys.exit(f"exe 不存在: {exe}")
    target = os.path.abspath(args.target)

    digest = sha256(exe)
    size = os.path.getsize(exe)
    mtime = __import__("datetime").datetime.fromtimestamp(os.path.getmtime(exe)).strftime("%Y-%m-%d %H:%M:%S")

    # 覆盖式发布：清空目标目录
    if os.path.isdir(target):
        for name in os.listdir(target):
            p = os.path.join(target, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        print(f"已清空: {target}")
    else:
        os.makedirs(target)
        print(f"已创建: {target}")

    shutil.copy2(exe, os.path.join(target, EXE_NAME))
    with open(os.path.join(target, "使用说明.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(README.format(ver=args.version, exe=EXE_NAME, size=size, sha=digest))
    with open(os.path.join(target, "版本信息.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(VERSION_TXT.format(ver=args.version, mtime=mtime, exe=EXE_NAME, size=size, sha=digest))
    with open(os.path.join(target, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(f"{digest}  {EXE_NAME}  {size}\n")

    # 自检：只允许纯净文件
    bad = []
    for name in sorted(os.listdir(target)):
        p = os.path.join(target, name)
        if os.path.isdir(p):
            bad.append(name + "/ (目录)")
            continue
        ext = os.path.splitext(name)[1].lower()
        if name not in ALLOWED_NAMES or ext in FORBIDDEN_EXT:
            bad.append(name)
    print("目录内容:", sorted(os.listdir(target)))
    if bad:
        sys.exit(f"[失败] 发现不允许的文件: {bad}")
    print("纯净性自检: 通过（无源码/构建文件）")
    print(f"版本 {args.version}  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
