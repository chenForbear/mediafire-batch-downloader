#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联网实测（默认跳过，避免 CI 里打真网络）。

跑法：
    set MFD_LIVE=1 && python tests/test_live.py

覆盖三件事，都是"离线测不出来、一上线就翻车"的地方：
  1. 文件夹解析：接口翻页、大小、文件名是否都拿得到。
  2. 直链解析：MediaFire 的临时直链能不能取到（403 的高发点）。
  3. 真实下载：Range 续传请求是否被接受、总长是否与页面一致。

注意：第 3 项只下到 2 MB 就主动停，不会把几百 MB 拖下来。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LIVE = os.environ.get("MFD_LIVE") == "1"
FOLDER_URL = os.environ.get(
    "MFD_TEST_FOLDER", "https://www.mediafire.com/folder/j8ok4l49nwgcc")

requires_live = unittest.skipUnless(
    LIVE, "联网实测：设 MFD_LIVE=1 才跑（避免 CI 打真网络）")


@requires_live
class TestLiveParse(unittest.TestCase):
    def test_folder_parses(self):
        from mfd.mediafire import resolve_link
        items, description = resolve_link(FOLDER_URL, include_subfolders=False)
        self.assertTrue(items, "解析不出文件：%r" % description)
        for item in items[:20]:
            self.assertTrue(item["name"], "文件名为空")
            self.assertGreater(item["size"], 0, "大小为 0：%s" % item["name"])
            self.assertIn("/file/", item["url"])
        print("\n  [解析] %s，首个：%s (%d 字节)"
              % (description, items[0]["name"], items[0]["size"]))

    def test_direct_link_and_range(self):
        from mfd.mediafire import resolve_link, resolve_download_url, http_get
        items, _ = resolve_link(FOLDER_URL, include_subfolders=False)
        first = items[0]
        link, page_size = resolve_download_url(first["url"])
        self.assertTrue(link.startswith("http"), "没拿到直链：%r" % (link,))

        response, raw = http_get(link, headers={"Range": "bytes=0-65535"}, timeout=30)
        try:
            self.assertIn(response.status, (200, 206))
            content_range = response.headers.get("Content-Range") or ""
            self.assertTrue(content_range, "没有 Content-Range，续传会失效")
            total = int(content_range.split("/")[-1])
            # 关键断言：页面给的大小必须和服务器总长一致，否则会误判"大小不符"
            self.assertEqual(total, first["size"],
                             "页面大小 %d 与服务器总长 %d 不一致" % (first["size"], total))
            self.assertEqual(len(raw), 65536)
            print("\n  [直链] HTTP %s，总长 %d，直链解析给的大小 %s"
                  % (response.status, total, page_size))
        finally:
            response.close()


@requires_live
class TestLiveDownload(unittest.TestCase):
    def test_partial_download(self):
        from mfd.downloader import Downloader, STATUS_DONE, STATUS_FAILED
        from mfd.mediafire import resolve_link
        from mfd.storage import History

        workdir = tempfile.mkdtemp(prefix="mfd-live-")
        self.addCleanup(lambda: shutil.rmtree(workdir, ignore_errors=True))
        events = []

        def on_event(event, payload):
            if event in ("log", "failure"):
                events.append((event, str(payload)[:160]))

        try:
            items, _ = resolve_link(FOLDER_URL, include_subfolders=False)
            downloader = Downloader(workdir, history=History(enabled=False),
                                    max_threads=1, gap_seconds=0, on_event=on_event)
            downloader.load_tasks(items[:1])
            self.assertTrue(downloader.start(), "启动失败")

            target = 2 * 1024 * 1024
            deadline = time.time() + 120
            task = downloader.tasks[0]
            while time.time() < deadline:
                if task.status in (STATUS_DONE, STATUS_FAILED):
                    break
                if task.downloaded >= target:
                    break
                time.sleep(0.2)
            downloader.stop_and_wait(10)

            self.assertGreater(task.downloaded, 0,
                               "一字节都没下到，事件：%r" % (events[:6],))
            self.assertNotEqual(task.status, STATUS_FAILED,
                                "下载失败：%s / %s" % (task.fail_kind, task.error))
            print("\n  [下载] %s 已下 %d 字节，状态=%s，速度=%.0f KB/s"
                  % (task.name, task.downloaded, task.status, task.speed))
        finally:
            try:
                downloader.stop_and_wait(5)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
