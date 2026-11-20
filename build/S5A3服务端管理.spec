# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    [str(SPECPATH + '/../src/s5a3_deploy_tool.py')],
    pathex=[str(SPECPATH + '/../src')],
    binaries=[],
    datas=[('S5A3BG.jpg', '.')],
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
