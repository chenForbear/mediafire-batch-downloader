# -*- coding: utf-8 -*-
"""把 dist-dir 里的目录版打成发布用 zip。

为什么不直接右键压缩：Windows 资源管理器打的 zip 路径分隔符是反斜杠，
Linux/macOS 解出来是一坨带反斜杠文件名的文件；用 .NET 的
``ZipFile.CreateFromDirectory`` 又会多裹一层目录。这里手工写，保证
条目顺序稳定、分隔符一律 ``/``、不带外层目录（解压时资源管理器会用
zip 名自动建文件夹，正好）。
"""
from __future__ import print_function

import os
import sys
import zipfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(PROJECT_DIR, "dist-dir", "MediaFire批量下载器2.0")
DEFAULT_DST = os.path.join(PROJECT_DIR, "dist-package",
                           "MediaFire-Downloader-2.0.3-portable-win64.zip")


def collect(source):
    entries = []
    for current, dirs, files in os.walk(source):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, source).replace(os.sep, "/")
            entries.append((full, rel))
    return entries


def main(argv):
    source = argv[1] if len(argv) > 1 else DEFAULT_SRC
    target = argv[2] if len(argv) > 2 else DEFAULT_DST
    if not os.path.isdir(source):
        print("找不到目录：%s" % source)
        return 2
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    entries = collect(source)
    handle = zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
    try:
        for full, rel in entries:
            handle.write(full, rel)
    finally:
        handle.close()
    with zipfile.ZipFile(target) as check:
        bad = [n for n in check.namelist() if "\\" in n]
        total = len(check.namelist())
    size = os.path.getsize(target)
    print("源目录：%s" % source)
    print("输出：%s" % target)
    print("条目：%d，反斜杠条目：%d，大小：%.2f MB" % (total, len(bad), size / 1048576.0))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
