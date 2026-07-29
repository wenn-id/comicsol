# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

product_datas, product_binaries, product_hidden = collect_all('comic_sol_product')
mcp_datas, mcp_binaries, mcp_hidden = collect_all(
    'mcp', filter_submodules=lambda name: not name.startswith('mcp.cli')
)
pillow_datas, pillow_binaries, pillow_hidden = collect_all('PIL')

a = Analysis(
    ['entrypoint.py'],
    pathex=[],
    binaries=product_binaries + mcp_binaries + pillow_binaries,
    datas=product_datas + mcp_datas + pillow_datas,
    hiddenimports=product_hidden + mcp_hidden + pillow_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pkg_resources', 'setuptools'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='comic-sol',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='comic-sol',
)
