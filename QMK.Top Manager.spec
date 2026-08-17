# -*- mode: python ; coding: utf-8 -*-
import os
import importlib.util
from PyInstaller.utils.hooks import collect_all

datas = [
    ('assets/qmk-top-manager-keyboard.ico', 'assets'),
    ('sniffer.js', '.'),
    ('vendor/flet-windows.zip', 'flet_desktop/app'),
]
binaries = []
hiddenimports = ['pystray._win32', 'PIL._tkinter_finder', 'hid']

# hidapi ships hid as a .pyd — ensure it's bundled
_hid_spec = importlib.util.find_spec('hid')
if _hid_spec and _hid_spec.origin:
    binaries.append((_hid_spec.origin, '.'))

tmp_ret = collect_all('flet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('flet_desktop')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

tmp_ret = collect_all('winotify')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app_flet.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='QMK.Top Manager for SK75 TMR',
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
    icon=['assets\\qmk-top-manager-keyboard.ico'],
)
