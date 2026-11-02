# -*- coding: utf-8 -*-
"""2.0.3 修掉的三件事的回归测试。

这三条都是"用户看到的现象和真正的原因对不上"造成的返工，必须钉死：

  1. ``start()`` 启动失败时不能发 ``finished`` —— 一发界面就弹「下载结束 完成 0」，
     用户以为跑完了，其实是压根没跑；
  2. 用户改了保存位置，引擎得跟着换 —— 否则它拿老目录判权限，报「没有写入权限」；
  3. 历史记录命中但文件已经不在盘上，必须重新下 —— 不能因为"接口给了大小"就跳过。
"""
import os
import sys
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:          # pragma: no cover
    import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mfd.downloader import Downloader, STATUS_SKIPPED, STATUS_WAITING
from mfd.storage import History


def _make_downloader(directory, items=None):
    events = []
    downloader = Downloader(download_dir=directory,
                            history=History(enabled=False),
                            on_event=lambda event, payload: events.append(event))
    downloader.load_tasks(items or [{"name": "a.zip", "url": "http://x/a", "size": 10}])
    return downloader, events


class TestStartDoesNotFakeFinish(unittest.TestCase):
    def test_no_pending_does_not_emit_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader, events = _make_downloader(tmp)
            for task in downloader.tasks:
                task.status = STATUS_SKIPPED
            started = downloader.start()
            self.assertFalse(started)
            self.assertNotIn("finished", events)      # 不能冒充"正常结束"
            self.assertIn("没有待下载", downloader.last_error)

    def test_unwritable_dir_does_not_emit_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader, events = _make_downloader(tmp)
            with mock.patch("mfd.downloader.probe_writable_dir",
                            return_value=(False, "文件夹是只读的")):
                started = downloader.start()
            self.assertFalse(started)
            self.assertNotIn("finished", events)
            self.assertIn("保存目录无法写入", downloader.last_error)


class TestSetDownloadDir(unittest.TestCase):
    def test_switching_clears_cache_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader, _ = _make_downloader(tmp)
            downloader._writable_cache[downloader.download_dir] = (False, "旧的失败")
            downloader.last_error = "旧的错误"
            target = os.path.join(tmp, "另一个目录")
            self.assertTrue(downloader.set_download_dir(target))
            self.assertEqual(downloader.download_dir, target)
            self.assertEqual(downloader._writable_cache, {})
            self.assertEqual(downloader.last_error, "")

    def test_same_or_empty_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader, _ = _make_downloader(tmp)
            self.assertFalse(downloader.set_download_dir(tmp))
            self.assertFalse(downloader.set_download_dir(""))


class TestHistorySkipNeedsRealFile(unittest.TestCase):
    def _downloader_with_history(self, directory, disk_has_file):
        downloader = Downloader(download_dir=directory, history=History(enabled=False))
        downloader.load_tasks([{"name": "a.zip", "url": "http://x/a",
                                "size": 10, "quickkey": "q1"}])
        return downloader

    def test_history_hit_without_file_still_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader = Downloader(download_dir=tmp, history=History(enabled=False))
            with mock.patch.object(downloader.history, "has", return_value=True):
                downloader.load_tasks([{"name": "a.zip", "url": "http://x/a",
                                        "size": 10, "quickkey": "q1"}])
            self.assertEqual(downloader.tasks[0].status, STATUS_WAITING)

    def test_history_hit_with_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.zip"), "wb") as handle:
                handle.write(b"x" * 10)
            downloader = Downloader(download_dir=tmp, history=History(enabled=False))
            with mock.patch.object(downloader.history, "has", return_value=True):
                downloader.load_tasks([{"name": "a.zip", "url": "http://x/a",
                                        "size": 10, "quickkey": "q1"}])
            self.assertEqual(downloader.tasks[0].status, STATUS_SKIPPED)


if __name__ == "__main__":
    unittest.main()
