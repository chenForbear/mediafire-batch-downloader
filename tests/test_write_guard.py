# -*- coding: utf-8 -*-
"""写权限守卫的单元测试。

覆盖三样东西：
  1. ``probe_writable_dir()`` 能不能真的分辨「能写」和「不能写」；
  2. ``describe_write_error()`` 有没有把系统异常翻成人话；
  3. 保存目录写不进去时，引擎会不会当场拦下并给出原因。
"""
import errno
import os
import sys
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:          # pragma: no cover - Python 2 时代的老习惯
    import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mfd import util
from mfd.downloader import Downloader
from mfd.storage import History


class TestProbeWritableDir(unittest.TestCase):
    def test_normal_dir_is_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = util.probe_writable_dir(tmp)
            self.assertTrue(ok)
            self.assertEqual(reason, "")
            # 探测文件必须自己清掉，不能在用户目录里留垃圾
            self.assertEqual(os.listdir(tmp), [])

    def test_empty_path_is_rejected(self):
        ok, reason = util.probe_writable_dir("")
        self.assertFalse(ok)
        self.assertIn("为空", reason)

    def test_missing_dir_gets_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "新建", "子目录")
            ok, reason = util.probe_writable_dir(target)
            self.assertTrue(ok, reason)
            self.assertTrue(os.path.isdir(target))

    def test_path_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "已经是文件.txt")
            open(path, "w").close()
            ok, reason = util.probe_writable_dir(path)
            self.assertFalse(ok)
            self.assertTrue(reason)


class TestDescribeWriteError(unittest.TestCase):
    def test_missing_directory(self):
        msg = util.describe_write_error("D:/不存在的目录/a.zip.part",
                                        FileNotFoundError(2, "找不到"))
        self.assertIn("保存目录不存在", msg)
        self.assertIn("不存在的目录", msg)

    def test_disk_full(self):
        msg = util.describe_write_error("/tmp/a.zip.part",
                                        OSError(errno.ENOSPC, "no space"))
        self.assertIn("磁盘空间不足", msg)

    def test_name_too_long(self):
        msg = util.describe_write_error("/tmp/a.zip.part",
                                        OSError(errno.ENAMETOOLONG, "too long"))
        self.assertIn("文件名太长", msg)

    def test_permission_but_dir_is_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            msg = util.describe_write_error(os.path.join(tmp, "a.zip.part"),
                                            PermissionError(errno.EACCES, "denied"))
            self.assertIn("被系统拒绝", msg)

    def test_permission_and_dir_unwritable(self):
        with mock.patch.object(util, "probe_writable_dir",
                               return_value=(False, "文件夹是只读的")):
            msg = util.describe_write_error("/tmp/x/a.zip.part",
                                            PermissionError(errno.EACCES, "denied"))
            self.assertIn("写不进", msg)
            self.assertIn("文件夹是只读的", msg)


class TestStartRefusesUnwritableDir(unittest.TestCase):
    def _make_downloader(self, directory):
        downloader = Downloader(download_dir=directory, history=History(enabled=False))
        downloader.load_tasks([{"name": "a.zip", "url": "http://example.invalid/a.zip",
                                "size": 10}])
        return downloader

    def test_start_returns_false_and_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader = self._make_downloader(tmp)
            with mock.patch("mfd.downloader.probe_writable_dir",
                            return_value=(False, "没有写入权限")):
                started = downloader.start()
            self.assertFalse(started)
            self.assertIn("保存目录无法写入", downloader.last_error)
            self.assertIn("没有写入权限", downloader.last_error)

    def test_last_error_is_cleared_on_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader = self._make_downloader(tmp)
            downloader.last_error = "上一轮的旧错误"
            with mock.patch("mfd.downloader.probe_writable_dir",
                            return_value=(True, "")):
                downloader.start()
            self.assertEqual(downloader.last_error, "")
            downloader.stop_and_wait(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
