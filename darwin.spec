# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

app_name = 'GPUStack'

datas = [
    ('./gpustack/migrations', './gpustack/migrations'), 
    ('./gpustack/assets', './gpustack/assets'), 
    ('./gpustack/ui', './gpustack/ui'), 
    ('./gpustack/third_party', './gpustack/third_party'),
    ('./gpustack/detectors/fastfetch/*.jsonc', './gpustack/detectors/fastfetch/'),
    ('./installation/ai.gpustack.plist', '.'),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('aiosqlite')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['gpustack/main.py'],
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
    [],
    exclude_binaries=True,
    name=app_name.lower(),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='main',
)

# 创建 .app 包
app = BUNDLE(
    coll,  # 将 coll 放入 BUNDLE 中
    name=f'{app_name}.app',
    icon=f'./installation/{app_name}.icns',  # 图标文件路径
    bundle_identifier='ai.gpustack.gpustack',  # 应用标识符
    info_plist={
        'CFBundleName': app_name,
        'CFBundleDisplayName': app_name,
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSHumanReadableCopyright': 'Copyright © 2025 Your Company',
        'LSMinimumSystemVersion': '14.0',  # 最低系统要求
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
    },
)
