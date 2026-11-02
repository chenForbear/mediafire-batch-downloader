# -*- coding: utf-8 -*-
"""建 GitHub Release 并上传资产（走 REST API，不依赖 gh CLI）。

用法::

    python tools/publish_release.py

token 从系统凭据管理器里取（`git credential fill`），不落盘、不打印。
"""
from __future__ import print_function

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "chenForbear/mediafire-batch-downloader"
TAG = "v2.1.0"
NAME = "2.1.0 —— 按文件夹名建子目录，20 项新功能 + 20 处优化"
BODY_FILE = os.path.join(PROJECT_DIR, "dist-package", "release-body-210.md")
ASSETS = [
    os.path.join(PROJECT_DIR, "dist-package",
                 "MediaFire-Downloader-2.1.0-portable-win64.zip"),
    os.path.join(PROJECT_DIR, "dist-package",
                 "mediafire-batch-downloader-2.1.0-source.zip"),
]
API = "https://api.github.com"


def token():
    proc = subprocess.Popen(["git", "credential", "fill"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    out, _ = proc.communicate(b"protocol=https\nhost=github.com\n\n")
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("取不到 GitHub 凭据，先确认 git credential manager 里存着 token")


def api(url, tok, payload=None, method="GET", timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "token " + tok)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "mfd-release-script")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def upload(url, tok, path, attempts=3):
    size = os.path.getsize(path)
    for attempt in range(1, attempts + 1):
        with open(path, "rb") as handle:
            blob = handle.read()
        request = urllib.request.Request(url, data=blob, method="POST")
        request.add_header("Authorization", "token " + tok)
        request.add_header("Content-Type", "application/octet-stream")
        request.add_header("User-Agent", "mfd-release-script")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            print("  上传第 %d 次失败：%s" % (attempt, exc))
            if attempt == attempts:
                raise
            time.sleep(3 * attempt)
    return {}


def main():
    tok = token()
    with open(BODY_FILE, encoding="utf-8") as handle:
        body = handle.read()
    release = api("%s/repos/%s/releases" % (API, REPO), tok, {
        "tag_name": TAG,
        "target_commitish": "main",
        "name": NAME,
        "body": body,
        "draft": False,
        "prerelease": False,
    }, method="POST")
    print("Release 建好了：id=%s tag=%s" % (release["id"], release["tag_name"]))
    template = release["upload_url"].split("{")[0]
    for path in ASSETS:
        name = os.path.basename(path)
        url = "%s?name=%s" % (template, urllib.parse.quote(name))
        asset = upload(url, tok, path)
        print("  资产 %s → state=%s size=%s" % (asset.get("name"),
                                                asset.get("state"),
                                                asset.get("size")))
    print("页面：https://github.com/%s/releases/tag/%s" % (REPO, TAG))
    return 0


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
