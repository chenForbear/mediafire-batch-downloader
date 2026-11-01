#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据目录的唯一来源。

为什么单列一个模块
------------------
失败清单、下载历史、诊断报告、崩溃日志这四样东西原本各自拼路径，结果出现过
"历史在 ``MediaFireDownloader``、失败清单在 ``MediaFireDownloader2``"这种
差一个字符的错误——排障时最怕的就是找错目录。现在只在这里算一次。

两个可覆盖点（都是为测试和便携场景准备的）：

* 环境变量 ``MFD_DATA_DIR``：直接指定数据目录，**优先级最高**。
  便携版（放 U 盘）或受限环境下用它；单元测试也用它把数据写进临时目录，
  免得污染用户真正的 ``%LOCALAPPDATA%``。
* 默认：``%LOCALAPPDATA%\\MediaFireDownloader2``（Windows），
  其他系统退到 ``~/.mediafiredownloader2``。
"""

import os

APP_DIR_NAME = "MediaFireDownloader2"
ENV_OVERRIDE = "MFD_DATA_DIR"


def data_dir(create=True):
    """返回本程序的数据目录；``create=True`` 时顺手建出来。"""
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            path = os.path.join(base, APP_DIR_NAME)
        else:
            path = os.path.join(os.path.expanduser("~"), "." + APP_DIR_NAME.lower())
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            # 建不出来不算致命：调用方写文件时还会再失败一次，那里会给出具体原因
            pass
    return path


def data_file(name, create=True):
    return os.path.join(data_dir(create=create), name)


def sub_dir(name, create=True):
    return data_dir(create=create) if not name else data_file(name, create=create)
