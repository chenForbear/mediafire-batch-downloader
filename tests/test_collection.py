#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集合子目录（collection_name）的行为测试。

背景（用户 2026-11-02 的需求）：选中的保存位置应当只当"父目录"，
真正落盘的地方是它下面以 MediaFire 文件夹名命名的子目录，
这样一次下载一个合集不会把父目录糊成一片。
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover - Python 3.8 有 unittest.mock
    import mock

from mfd import mediafire
from mfd.downloader import Downloader, DownloadTask
from mfd.storage import History


def _make(root):
    """造一个不碰网络的下载器：禁用历史记录，事件丢弃。"""
    return Downloader(root, history=History(enabled=False), on_event=lambda e, p: None)


class TestEffectiveRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mfd-coll-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_collection_keeps_old_behaviour(self):
        """没设集合名时，文件就放在保存位置本身（跟以前一样）。"""
        dl = _make(self.root)
        self.assertEqual(dl.effective_root(), self.root)
        self.assertEqual(dl._effective_root(), self.root)

    def test_collection_appends_subdirectory(self):
        dl = _make(self.root)
        self.assertTrue(dl.set_collection_name("澳洲客栈"))
        self.assertEqual(dl.collection_name, "澳洲客栈")
        self.assertEqual(dl._effective_root(), os.path.join(self.root, "澳洲客栈"))

    def test_set_collection_name_sanitizes(self):
        """文件夹名里的非法字符必须被清洗掉，否则建目录会失败。"""
        dl = _make(self.root)
        dl.set_collection_name('合集:第1话/上<下>|"*?"')
        self.assertNotIn(":", dl.collection_name)
        self.assertNotIn("/", dl.collection_name)
        self.assertNotIn("|", dl.collection_name)
        self.assertTrue(dl.collection_name)

    def test_set_collection_name_returns_false_when_unchanged(self):
        """同名重复设置不该反复记日志、也不该清缓存。"""
        dl = _make(self.root)
        self.assertTrue(dl.set_collection_name("A"))
        self.assertFalse(dl.set_collection_name("A"))
        # 全是空白等同于清空，是一次真实的变化
        self.assertTrue(dl.set_collection_name("   "))
        self.assertFalse(dl.set_collection_name(""))

    def test_clearing_collection(self):
        dl = _make(self.root)
        dl.set_collection_name("A")
        self.assertTrue(dl.set_collection_name(""))
        self.assertEqual(dl.collection_name, "")
        self.assertEqual(dl._effective_root(), self.root)


class TestCollectionWithFolder(unittest.TestCase):
    """集合子目录与「保留 MediaFire 内部子文件夹」叠加时的路径层级。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mfd-coll2-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_nested_folder_goes_under_collection(self):
        dl = _make(self.root)
        dl.set_collection_name("合集")
        target = os.path.join(dl.effective_root(), "第一卷")
        os.makedirs(target)
        task = DownloadTask(name="a.zip", url="http://example.invalid/a",
                            folder=os.path.join("第一卷", ""))
        task.saved_path = os.path.join(target, "a.zip")
        with open(task.saved_path, "wb") as handle:
            handle.write(b"x" * 32)
        self.assertTrue(dl._file_already_on_disk(task))

    def test_file_in_parent_dir_is_not_counted(self):
        """旧版本直接下在父目录里的文件，不该被新逻辑误判成"已存在"。"""
        dl = _make(self.root)
        dl.set_collection_name("合集")
        task = DownloadTask(name="a.zip", url="http://example.invalid/a")
        legacy = os.path.join(self.root, "a.zip")
        with open(legacy, "wb") as handle:
            handle.write(b"x" * 32)
        self.assertFalse(dl._file_already_on_disk(task))


class TestCollectionDownload(unittest.TestCase):
    """真下载一遍：验证文件确实落在「父目录/集合名」里，而不是父目录本身。"""

    PAYLOAD = b"hello-collection" * 4096

    @classmethod
    def setUpClass(cls):
        import http.server
        payload = cls.PAYLOAD

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/payload.bin" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mfd-coll3-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, collection):
        events = []
        dl = Downloader(self.root, history=History(enabled=False),
                        on_event=lambda e, p: events.append(e), max_threads=1,
                        gap_seconds=0.0)
        dl.set_collection_name(collection)
        dl.load_tasks([{"name": "payload.bin", "url": self.url,
                        "size": len(self.PAYLOAD), "quickkey": "qk1"}])
        with mock.patch.object(mediafire, "resolve_download_url",
                               return_value=(self.url, len(self.PAYLOAD))):
            dl.start()
            # 等 finished 事件，而不是傻等：stop_and_wait 立刻调用会把还没下完的任务标成取消。
            deadline = time.time() + 30.0
            while time.time() < deadline and "finished" not in events:
                time.sleep(0.05)
            dl.stop_and_wait(30.0)
        return dl, events

    def test_downloads_into_collection_subdir(self):
        dl, _events = self._run("澳洲客栈")
        task = dl.tasks[0]
        self.assertEqual(task.status, "done", task.error)
        expected = os.path.join(self.root, "澳洲客栈", "payload.bin")
        self.assertTrue(os.path.isfile(expected), task.saved_path)
        with open(expected, "rb") as handle:
            self.assertEqual(handle.read(), self.PAYLOAD)
        self.assertFalse(os.path.exists(os.path.join(self.root, "payload.bin")),
                         "文件不该直接落在父目录里")

    def test_downloads_into_parent_when_disabled(self):
        dl, _events = self._run("")
        task = dl.tasks[0]
        self.assertEqual(task.status, "done", task.error)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "payload.bin")))


if __name__ == "__main__":
    unittest.main()