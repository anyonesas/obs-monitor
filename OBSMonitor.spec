# -*- mode: python ; coding: utf-8 -*-
import sys, os

block_cipher = None
BASE = os.path.dirname(os.path.abspath(SPEC))

TKINTER_SO  = '/opt/homebrew/opt/python-tk@3.14/libexec/_tkinter.cpython-314-darwin.so'
TCL_DYLIB   = '/opt/homebrew/opt/tcl-tk/lib/libtcl9.0.dylib'
TK_DYLIB    = '/opt/homebrew/opt/tcl-tk/lib/libtcl9tk9.0.dylib'
TCL_LIB_DIR = '/opt/homebrew/opt/tcl-tk/lib/tcl9.0'
TK_LIB_DIR  = '/opt/homebrew/opt/tcl-tk/lib/tk9.0'

# Collecte les fichiers de données Tcl/Tk (thèmes, encodages, etc.)
import glob
tcl_datas = []
for path in glob.glob(TCL_LIB_DIR + '/**/*', recursive=True):
    if os.path.isfile(path):
        rel = os.path.relpath(os.path.dirname(path), '/opt/homebrew/opt/tcl-tk/lib')
        tcl_datas.append((path, rel))
for path in glob.glob(TK_LIB_DIR + '/**/*', recursive=True):
    if os.path.isfile(path):
        rel = os.path.relpath(os.path.dirname(path), '/opt/homebrew/opt/tcl-tk/lib')
        tcl_datas.append((path, rel))

a = Analysis(
    [os.path.join(BASE, 'app.py')],
    pathex=[BASE],
    binaries=[
        (TKINTER_SO, '.'),
        (TCL_DYLIB,  '.'),
        (TK_DYLIB,   '.'),
    ],
    datas=[
        (os.path.join(BASE, 'config.json'), '.'),
        (os.path.join(BASE, 'assets', 'OBSMonitor.icns'), 'assets'),
    ] + tcl_datas,
    hiddenimports=[
        'rumps',
        'obsws_python',
        'websocket',
        'Quartz',
        'Quartz.CoreGraphics',
        'AppKit',
        'Foundation',
        'PyObjCTools',
        'PyObjCTools.AppHelper',
        'objc',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OBSMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(BASE, 'assets', 'OBSMonitor.icns'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OBSMonitor',
)

app = BUNDLE(
    coll,
    name='OBSMonitor.app',
    icon=os.path.join(BASE, 'assets', 'OBSMonitor.icns'),
    bundle_identifier='com.obsmonitor.app',
    info_plist={
        'NSHighResolutionCapable': True,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleName': 'OBS Monitor',
        'CFBundleDisplayName': 'OBS Monitor',
    },
)
