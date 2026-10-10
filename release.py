"""
发布打包脚本

把 PyInstaller 产物整理成可直接上传 Gitee 发行版的 zip。

步骤：
  1. 清理运行期生成的本地文件（config.ini / 数据库 / 错误日志）
     —— 这些是应用运行后自己产生的，绝不能带进发布包
        （config.ini 里将来可能存有用户的 Cookie）；
  2. 压缩为 VideoDownloader-win64.zip。

用法：
  python build.py     # 先打包
  python release.py   # 再生成发布包
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist" / "VideoDownloader"
OUT = ROOT / "dist" / "VideoDownloader-win64.zip"

# 运行期生成、不入发布包的文件
_RUNTIME_JUNK = ("config.ini", "download_history.db", "startup_error.log")
_EXCLUDE_EXTS = {".ini", ".db", ".log"}


def main() -> int:
    if not (DIST / "VideoDownloader.exe").exists():
        print("[--] 未找到打包产物，请先运行 build.py")
        return 1

    # 1. 清理运行期垃圾
    for name in _RUNTIME_JUNK:
        path = DIST / name
        if path.exists():
            path.unlink()
            print(f"  已清理运行期文件: {name}")

    # 2. 压缩
    if OUT.exists():
        OUT.unlink()

    total = 0
    raw = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _dirs, files in os.walk(DIST):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in _EXCLUDE_EXTS or name in _RUNTIME_JUNK:
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, DIST)
                z.write(full, os.path.join("VideoDownloader", rel))
                total += 1
                raw += os.path.getsize(full)

    size = OUT.stat().st_size
    print(f"  文件数: {total}，原始 {raw / 1048576:.1f} MB -> 压缩后 {size / 1048576:.1f} MB")
    print(f"  输出: {OUT}")
    ok = size < 100 * 1048576
    print("  " + ("[OK] 可上传 Gitee 发行版（单附件 < 100MB）"
                  if ok else "[--] 超过 Gitee 单附件 100MB 限制，需裁剪"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
