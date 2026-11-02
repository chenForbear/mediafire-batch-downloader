#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MediaFire 批量下载器 2.0 —— 内核包。

设计边界（为什么这样切）
------------------------
* ``mediafire``    只负责“和 MediaFire 说话”：解析链接、列文件夹、取直链。
* ``downloader``   只负责“和磁盘说话”：排队、续传、校验、重试。
* ``failures``     只负责“记住失败”：失败清单、错误分类、导出、重试重建。
* ``diagnostics``  只负责“记住环境”：系统/报错两维归集，供用户反馈问题。
* ``storage``      只负责“记住历史”：下过什么，避免重复下载。
* ``ui``           只负责“和用户说话”：全部界面代码，不含业务逻辑。

除界面外全部模块只依赖 Python 标准库，Windows 7 SP1 + Python 3.8 即可运行。
"""

__version__ = "2.0.6"
__version_info__ = (2, 0, 6, 0)

APP_NAME = "MediaFire 批量下载器"
APP_ID = "MediaFireDownloader2"
APP_TAGLINE = "批量解析 · 断点续传 · 失败清单"
PROJECT_URL = "https://github.com/chenForbear/mediafire-batch-downloader"

__all__ = [
    "__version__",
    "__version_info__",
    "APP_NAME",
    "APP_ID",
    "APP_TAGLINE",
    "PROJECT_URL",
]
