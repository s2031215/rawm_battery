# -*- mode: python ; coding: utf-8 -*-

# Shared build spec — one file for every release. Bump VERSION here (and in
# version.txt and APP_VERSION in rawm_tray.py) and rebuild with:
#     python -m PyInstaller --noconfirm rawm_battery.spec

VERSION = "1.0.2"

a = Analysis(
    ['rawm_tray.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32'],
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
    name=f'RAWM.SA-MH01-v{VERSION}-x64',
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
    version='version.txt',
    icon=['rawm.ico'],
)
