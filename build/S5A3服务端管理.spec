# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ssl

# Some build interpreters require additional public trust data from ssl.py.
# Preserve that policy in frozen builds rather than omitting its companion file.
ssl_module = Path(ssl.__file__)
ssl_companion = ssl_module.with_name('codex-rejected-cas.pem')
extra_data = []
if ssl_companion.is_file():
    extra_data.append((str(ssl_companion), '.'))
elif ssl_module.is_file() and 'codex-rejected-cas.pem' in ssl_module.read_text(encoding='utf-8'):
    raise RuntimeError('Required SSL companion data is missing in the build interpreter')


a = Analysis(
    [str(SPECPATH + '/../src/s5a3_deploy_tool.py')],
    pathex=[str(SPECPATH + '/../src')],
    binaries=[],
    datas=[('S5A3BG.jpg', '.'), ('../src/gateway_bootstrap.py', '.'), ('launcher-template.exe', '.'), *extra_data],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='S5A3服务端管理',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['S5A3工具图标.ico'],
)
