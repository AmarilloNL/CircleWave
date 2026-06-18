# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CircleWave. Build on Windows (or via the GitHub Actions
# workflow) with:  pyinstaller circlewave.spec
# Produces a single windowed dist/CircleWave.exe.

a = Analysis(
    ['circlewave.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],          # bundle the icon so the app can load it
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # trim clearly-unused, heavy Qt modules to keep the .exe smaller.
        'tkinter',
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel',
        'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
        'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtDesigner', 'PySide6.QtTest', 'PySide6.QtSql',
        'PySide6.QtBluetooth', 'PySide6.QtPositioning', 'PySide6.QtSensors',
        'PySide6.QtSerialPort', 'PySide6.QtNfc',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CircleWave',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                 # compress if UPX is available (optional)
    runtime_tmpdir=None,
    console=False,            # windowed app, no console window
    icon='icon.ico',
)
