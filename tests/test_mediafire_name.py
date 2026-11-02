#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``mfd.mediafire`` 的离线自检：文件夹显示名 + 子文件夹并发解析。

直接运行：

    python tests\\test_mediafire_name.py

**全程不联网**：所有 ``_api_get`` 调用都被打桩，返回的是内存里构造的假树。

覆盖范围
--------
1. ``folder_display_name``：链接自带的 ``/folder/<key>/<名称>`` 优先（含 ``%5B``
   转义、非法字符清洗），带查询参数/末尾斜杠/大小写混写的 ``Folder`` 也要认
2. 链接里没有名字才走 ``folder/get_info``；接口报错、返回残缺、抛任意异常
   → 一律返回空串，不往上抛
3. ``resolve_folder_meta`` 同时给出名字与 key，且不改 ``resolve_link`` 的返回结构
4. ``list_folder_files`` 的**并发结果与串行版本逐条一致**（含顺序），同一层的
   子文件夹确实并发拉取，同 key 子文件夹只拉一次，子文件夹出错照样上抛
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mfd import mediafire                                        # noqa: E402
from mfd.mediafire import MediaFireError                         # noqa: E402
from mfd.util import sanitize_filename                           # noqa: E402

FOLDER_KEY = "6vaa2w3l3hwe"
BASE = "https://www.mediafire.com"


# ======================================================================
# 一、文件夹显示名
# ======================================================================

class FolderDisplayNameTest(unittest.TestCase):
    """``folder_display_name`` 的三级优先级。"""

    def test_链接里的名字优先且不发请求(self):
        """真实的分享链接长这样：``/folder/6vaa2w3l3hwe/[松田啓智×藤原芳秀][拳兒]...``。"""
        raw = "[松田啓智×藤原芳秀][拳兒][玉皇朝][C.C]"
        url = "%s/folder/%s/%s" % (BASE, FOLDER_KEY, quote(raw, safe=""))
        with mock.patch.object(mediafire, "_api_get") as api:
            name = mediafire.folder_display_name(url)
        self.assertEqual(name, sanitize_filename(raw, ""))
        api.assert_not_called()          # 链接里有名字就不该再发一次 HTTP

    def test_转义字符被正确解码(self):
        """``%5B`` 这类转义要还原成 ``[``，否则拿它建目录会出现一堆百分号。"""
        url = "%s/folder/%s/%%5B%%E6%%8B%%B3%%E5%%85%%92%%5Dabc" % (BASE, FOLDER_KEY)
        self.assertEqual(mediafire.folder_display_name(url), "[拳兒]abc")

    def test_名字里的非法字符被清洗(self):
        """Windows 不允许 ``: * ?`` 出现在目录名里，落盘前就得换掉。"""
        raw = "a:b*c?d"
        url = "%s/folder/%s/%s" % (BASE, FOLDER_KEY, quote(raw, safe=""))
        name = mediafire.folder_display_name(url)
        self.assertEqual(name, sanitize_filename(raw, ""))
        for char in ':*?<>|"\\/':
            self.assertNotIn(char, name)

    def test_查询参数末尾斜杠与大小写混合(self):
        """``/Folder/KEY/名字/?x=1`` 也认——这三处都是实测会碰到的写法。"""
        url = "%s/Folder/%s/%%E5%%90%%8D%%E5%%AD%%97/?x=1&y=2" % (BASE, FOLDER_KEY)
        self.assertEqual(mediafire.folder_display_name(url), "名字")

    def test_只有key时才问接口(self):
        stub = {"folder_info": {"name": "我的文件夹"}}
        with mock.patch.object(mediafire, "_api_get", return_value=stub) as api:
            name = mediafire.folder_display_name("%s/folder/%s" % (BASE, FOLDER_KEY))
        self.assertEqual(name, "我的文件夹")
        api.assert_called_once_with("folder/get_info", {"folder_key": FOLDER_KEY})

    def test_末尾斜杠但没名字也走接口(self):
        """``/folder/KEY/`` 只有 key，不能把空串当成名字拿回去。"""
        stub = {"folder_info": {"name": "接口给的"}}
        with mock.patch.object(mediafire, "_api_get", return_value=stub) as api:
            name = mediafire.folder_display_name("%s/folder/%s/" % (BASE, FOLDER_KEY))
        self.assertEqual(name, "接口给的")
        self.assertTrue(api.called)

    def test_接口返回的名字也会清洗(self):
        stub = {"folder_info": {"name": "a/b:c"}}
        with mock.patch.object(mediafire, "_api_get", return_value=stub):
            name = mediafire.folder_display_name("%s/folder/%s" % (BASE, FOLDER_KEY))
        self.assertEqual(name, sanitize_filename("a/b:c", ""))

    def test_接口抛MediaFireError返回空串(self):
        with mock.patch.object(mediafire, "_api_get",
                               side_effect=MediaFireError("接口坏了", "error=129")):
            name = mediafire.folder_display_name("%s/folder/%s" % (BASE, FOLDER_KEY))
        self.assertEqual(name, "")

    def test_接口抛任意异常也返回空串(self):
        """拿不到名字是正常分支，不该让整次解析崩掉。"""
        with mock.patch.object(mediafire, "_api_get", side_effect=RuntimeError("炸了")):
            self.assertEqual(
                mediafire.folder_display_name("%s/folder/%s" % (BASE, FOLDER_KEY)), "")

    def test_接口返回残缺结构(self):
        for stub in ({}, {"folder_info": {}}, {"folder_info": {"name": ""}},
                     {"folder_info": None, "result": "Success"}):
            with mock.patch.object(mediafire, "_api_get", return_value=stub):
                self.assertEqual(
                    mediafire.folder_display_name("%s/folder/%s" % (BASE, FOLDER_KEY)), "",
                    "残缺响应应返回空串：%r" % (stub,))

    def test_显式给了folder_key就不必再解析链接(self):
        stub = {"folder_info": {"name": "外部给的"}}
        with mock.patch.object(mediafire, "_api_get", return_value=stub) as api:
            name = mediafire.folder_display_name("https://www.mediafire.com/folder/", "GIVEN")
        self.assertEqual(name, "外部给的")
        api.assert_called_once_with("folder/get_info", {"folder_key": "GIVEN"})

    def test_不是MediaFire链接返回空串且不发请求(self):
        with mock.patch.object(mediafire, "_api_get") as api:
            self.assertEqual(mediafire.folder_display_name("https://example.com/whatever"), "")
        api.assert_not_called()

    def test_空链接返回空串(self):
        with mock.patch.object(mediafire, "_api_get") as api:
            self.assertEqual(mediafire.folder_display_name(""), "")
            self.assertEqual(mediafire.folder_display_name(None), "")
        api.assert_not_called()


class ResolveFolderMetaTest(unittest.TestCase):
    """``resolve_folder_meta`` 是给界面用的便捷入口。"""

    def test_链接带名字(self):
        raw = "[拳兒][C.C]"
        url = "%s/folder/%s/%s" % (BASE, FOLDER_KEY, quote(raw, safe=""))
        with mock.patch.object(mediafire, "_api_get") as api:
            meta = mediafire.resolve_folder_meta(url)
        self.assertEqual(meta["key"], FOLDER_KEY)
        self.assertEqual(meta["name"], sanitize_filename(raw, ""))
        api.assert_not_called()

    def test_返回结构固定(self):
        with mock.patch.object(mediafire, "_api_get",
                               return_value={"folder_info": {"name": "接口"}}):
            meta = mediafire.resolve_folder_meta("%s/folder/%s" % (BASE, FOLDER_KEY))
        self.assertEqual(sorted(meta.keys()), ["key", "name"])
        self.assertEqual(meta, {"key": FOLDER_KEY, "name": "接口"})

    def test_不是MediaFire链接时两个字段都为空(self):
        with mock.patch.object(mediafire, "_api_get") as api:
            meta = mediafire.resolve_folder_meta("https://example.com/x")
        self.assertEqual(meta, {"key": "", "name": ""})
        api.assert_not_called()


# ======================================================================
# 二、子文件夹并发解析
# ======================================================================

#: 假的文件夹树：key → 自己有哪些文件、有哪些子文件夹。
#: 层次特意做成"三层 + 一个叶子"：ROOT → (SUB1, SUB2, SUB3)，SUB1 → SUB1_1。
FOLDER_TREE = {
    "ROOT": {
        "files": [("root1.bin", "q-root-1", 10), ("root2.bin", "q-root-2", 20)],
        "folders": [("SUB1", "子一"), ("SUB2", "子二"), ("SUB3", "子三")],
    },
    "SUB1": {
        "files": [("a1.bin", "q-a1", 1)],
        "folders": [("SUB1_1", "孙一")],
    },
    "SUB2": {
        "files": [("b1.bin", "q-b1", 2)],
        "folders": [],
    },
    "SUB3": {
        "files": [("c1.bin", "q-c1", 3), ("c2.bin", "q-c2", 4)],
        "folders": [],
    },
    "SUB1_1": {
        "files": [("a11.bin", "q-a11", 5)],
        "folders": [],
    },
}


def make_stub(tree, delay=0.0):
    """造一个假的 ``_api_get``，并返回统计用的 state。

    state 里记三样东西，都是断言要用的：

    * ``max_active``：同时在跑的请求峰数——用来证明"确实并发"，比掐时间可靠；
    * ``calls``：每次请求的 ``(folder_key, content_type)``——用来证明"同 key 只拉一次"；
    * ``lock``：统计用锁，别让计数自己竞争出花来。
    """
    state = {"active": 0, "max_active": 0, "calls": []}
    lock = threading.Lock()

    def stub(action, params):
        key = params.get("folder_key")
        content_type = params.get("content_type", "files")
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["calls"].append((key, content_type))
        try:
            if delay:
                time.sleep(delay)       # 睡一下，让并发真的重叠起来
            if action != "folder/get_content":
                raise MediaFireError("打桩里没有这个接口", "action=%s" % action)
            node = tree.get(key)
            if node is None:
                raise MediaFireError("文件夹不存在", "folder_key=%s" % key)
            if content_type == "folders":
                folders = [{"folderkey": sub_key, "name": sub_name}
                           for sub_key, sub_name in node["folders"]]
                return {"folder_content": {"folders": folders,
                                           "folder_count": len(folders)}}
            files = []
            for filename, quickkey, size in node["files"]:
                files.append({
                    "filename": filename,
                    "quickkey": quickkey,
                    "size": size,
                    "hash": "AB" * 32,
                    "links": {"normal_download": "https://download.example/%s" % quickkey},
                })
            return {"folder_content": {"files": files,
                                       "more_chunks": "no",
                                       "folder_count": len(node["folders"])}}
        finally:
            with lock:
                state["active"] -= 1

    return stub, state


def serial_list(tree, root_key):
    """串行对照实现：逻辑与改造前的 ``list_folder_files`` 一模一样。

    故意**不复用**生产代码里的遍历循环——那样等于自己跟自己比，比不出并发有没有改坏顺序。
    这里只复用单文件夹的翻页函数 ``_fetch_folder_page``（它没被改）。
    """
    collected = []
    visited = set()
    queue = [(root_key, "")]
    while queue:
        key, prefix = queue.pop(0)
        if key in visited:
            continue
        visited.add(key)
        files, subfolders = mediafire._fetch_folder_page(key, prefix, None)
        collected.extend(files)
        for sub_key, sub_prefix in subfolders:
            if sub_key not in visited:
                queue.append((sub_key, sub_prefix))
    return collected


class ListFolderFilesTest(unittest.TestCase):
    """递归列举：并发后结果必须跟串行一模一样。"""

    def test_并发结果与串行完全一致(self):
        """核心断言：文件集合相同 **且** 顺序相同。"""
        stub, _state = make_stub(FOLDER_TREE)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            expected = serial_list(FOLDER_TREE, "ROOT")
            actual = mediafire.list_folder_files("ROOT")

        self.assertEqual(
            sorted((item["name"], item["folder"]) for item in actual),
            sorted((item["name"], item["folder"]) for item in expected),
            "并发拿到的文件集合与串行不一致",
        )
        self.assertEqual([item["name"] for item in actual],
                         [item["name"] for item in expected],
                         "顺序被并发打乱了")

    def test_folder字段带上了子目录前缀(self):
        """``folder`` 决定文件落在哪个子目录里，逗号级别的错都不能有。"""
        stub, _state = make_stub(FOLDER_TREE)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            items = mediafire.list_folder_files("ROOT")
        by_name = dict((item["name"], item["folder"]) for item in items)
        self.assertEqual(by_name["root1.bin"], "")
        self.assertEqual(by_name["子一/a1.bin"], "子一")
        self.assertEqual(by_name["子一/孙一/a11.bin"], "子一/孙一")
        self.assertEqual(by_name["子三/c2.bin"], "子三")
        self.assertEqual(len(items), 7)

    def test_同一层子文件夹确实并发(self):
        """峰数 ≥ 2 就说明线程池真的在并行拉，而不是排队一个个来。"""
        stub, state = make_stub(FOLDER_TREE, delay=0.05)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            mediafire.list_folder_files("ROOT")
        self.assertGreaterEqual(state["max_active"], 2,
                                "子文件夹看起来还是串行拉的（峰数=%d）" % state["max_active"])

    def test_不递归时只取根目录(self):
        stub, _state = make_stub(FOLDER_TREE)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            items = mediafire.list_folder_files("ROOT", include_subfolders=False)
        self.assertEqual([item["name"] for item in items], ["root1.bin", "root2.bin"])

    def test_同一个子文件夹只拉一次(self):
        """两个子文件夹指向同一个 key 时，只算一次、文件也只出现一次。"""
        tree = {
            "ROOT": {"files": [], "folders": [("DUP", "甲"), ("DUP", "乙")]},
            "DUP": {"files": [("d.bin", "q-d", 1)], "folders": []},
        }
        stub, state = make_stub(tree)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            items = mediafire.list_folder_files("ROOT")
        self.assertEqual([item["name"] for item in items], ["甲/d.bin"])
        self.assertEqual(state["calls"].count(("DUP", "files")), 1)

    def test_子文件夹报错会往上抛(self):
        """串行版本会抛，并发版本也必须抛——不能悄悄少几个文件夹的文件。"""
        tree = {
            "ROOT": {"files": [("ok.bin", "q-ok", 1)], "folders": [("GONE", "没了")]},
        }
        stub, _state = make_stub(tree)
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            with self.assertRaises(MediaFireError):
                mediafire.list_folder_files("ROOT")

    def test_进度回调只增不减(self):
        stub, _state = make_stub(FOLDER_TREE, delay=0.01)
        seen = []
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            items = mediafire.list_folder_files("ROOT", on_progress=seen.append)
        self.assertEqual(seen, sorted(seen), "进度回调出现回退：%r" % (seen,))
        self.assertEqual(seen[-1], len(items))

    def test_每层收完只回调一次(self):
        """并发时不能四个线程各报一次，否则界面数字会乱跳。"""
        stub, _state = make_stub(FOLDER_TREE)
        seen = []
        with mock.patch.object(mediafire, "_api_get", side_effect=stub):
            mediafire.list_folder_files("ROOT", on_progress=seen.append)
        # 层数只有三层：根 → 三个子文件夹 → 孙文件夹（单文件夹路径内部会按 chunk 报）
        self.assertLessEqual(len(seen), 6, "回调次数异常：%r" % (seen,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
