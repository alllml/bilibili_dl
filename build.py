"""
自动化打包脚本
自动收集插件、生成 spec 文件、执行 PyInstaller 打包
"""
import os
import sys
import subprocess
from pathlib import Path

# 项目根目录
ROOT_DIR = Path(__file__).parent
PLUGINS_DIR = ROOT_DIR / "plugins"
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"

def collect_plugins():
    """自动收集所有插件文件"""
    plugin_files = []
    hidden_imports = []

    for py_file in PLUGINS_DIR.glob("*.py"):
        if py_file.name == "__init__.py":
            plugin_files.append((str(py_file), "plugins"))
        elif not py_file.name.startswith("_"):
            plugin_files.append((str(py_file), "plugins"))
            module_name = f"plugins.{py_file.stem}"
            hidden_imports.append(module_name)

    return plugin_files, hidden_imports

def generate_spec():
    """生成 PyInstaller spec 文件"""
    plugin_files, hidden_imports = collect_plugins()

    print(f"[OK] 发现 {len(hidden_imports)} 个插件:")
    for imp in hidden_imports:
        print(f"  - {imp}")

    # 构建 datas 列表（使用正斜杠避免转义问题）
    datas_str = ",\n        ".join(
        [f"(r'{src}', '{dst}')" for src, dst in plugin_files]
    )

    # 构建 hiddenimports 列表
    hidden_str = ",\n        ".join([f"'{imp}'" for imp in hidden_imports])

    # 检查图标和版本信息文件
    icon_path = ROOT_DIR / "icon.ico"
    version_path = ROOT_DIR / "version_info.txt"

    icon_param = f"icon=r'{icon_path}'" if icon_path.exists() else "icon=None"
    version_param = f",\n    version=r'{version_path}'" if version_path.exists() else ""

    if icon_path.exists():
        print(f"[OK] 找到图标: {icon_path}")
    else:
        print("[--] 未找到图标文件 icon.ico")

    if version_path.exists():
        print(f"[OK] 找到版本信息: {version_path}")
    else:
        print("[--] 未找到版本信息文件 version_info.txt")

    spec_content = f'''# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（自动生成）
由 build.py 自动生成，请勿手动编辑
"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        ('venv/Lib/site-packages/selenium/webdriver/common/windows/selenium-manager.exe',
         'selenium/webdriver/common/windows/'),
    ],
    datas=[
        {datas_str},
    ],
    hiddenimports=[
        {hidden_str},
        'selenium',
        'selenium.webdriver.edge.service',
        'selenium.webdriver.edge.webdriver',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.support.ui',
        'selenium.webdriver.support.expected_conditions',
        'selenium.webdriver.common.by',
        'selenium.webdriver.common.selenium_manager',
        'selenium.webdriver.common.driver_finder',
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    {icon_param}{version_param},
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoDownloader',
)
'''

    spec_path = ROOT_DIR / "VideoDownloader.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    print(f"[OK] 生成 spec 文件: {spec_path}")
    return spec_path

def build():
    """执行打包"""
    print("=" * 60)
    print("视频下载工具 - 自动打包脚本")
    print("=" * 60)

    # 检查虚拟环境
    if not (ROOT_DIR / "venv").exists():
        print("[--] 未找到 venv 虚拟环境")
        print("  请先创建虚拟环境: python -m venv venv")
        print("  并安装依赖: venv\\Scripts\\pip install -r requirements.txt")
        return 1

    # 生成 spec
    spec_path = generate_spec()

    # 清理旧的构建
    if BUILD_DIR.exists():
        print(f"\n清理旧构建目录: {BUILD_DIR}")
        import shutil
        shutil.rmtree(BUILD_DIR)

    # 执行 PyInstaller
    print("\n开始打包...")
    print("-" * 60)
    cmd = ["pyinstaller", "--clean", "-y", str(spec_path)]
    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode == 0:
        print("\n" + "=" * 60)
        print("[OK] 打包成功！")
        print(f"输出目录: {DIST_DIR / 'VideoDownloader'}")
        print(f"可执行文件: {DIST_DIR / 'VideoDownloader' / 'VideoDownloader.exe'}")
        print("=" * 60)
        print("\n注意事项:")
        print("  1. 需要安装 FFmpeg 到系统 PATH（用于 B站视频合并）")
        print("  2. 需要安装 Edge 或 Chrome 浏览器（用于抖音扫码登录）")
        return 0
    else:
        print("\n[--] 打包失败")
        return 1

if __name__ == "__main__":
    sys.exit(build())
