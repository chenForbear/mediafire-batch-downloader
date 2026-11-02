"""检查有没有新版本（打 GitHub Releases 的 latest 接口）。

界面上的"检查更新"要的是三件事：现在是不是最新、最新是哪一版、去哪儿看。
把这三件事交给一个函数，**调用方自己决定放不放线程**——本模块的每个函数都是纯的
（除了那一次 HTTP 请求），不碰界面、不弹窗、不写盘、不抛异常。

为什么不用第三方库：项目只有标准库这一条依赖红线（详见 README 的"零依赖"一节），
``urllib.request`` 已经够用了——一个 GET 请求加一次 JSON 解析而已。
"""

import json
import urllib.error
import urllib.request

from . import PROJECT_URL, __version__

__all__ = ["LATEST_API", "parse_version", "check_latest_version"]

#: GitHub 的"最新正式版"接口。它不会返回草稿和预发布版，正是我们想要的。
LATEST_API = "https://api.github.com/repos/chenForbear/mediafire-batch-downloader/releases/latest"


def parse_version(text):
    """``'v2.1.0'`` / ``'2.1.0'`` / ``'2.1'`` → ``(2,1,0)``；解析不了返回 ``()``。

    规则说明（都是实际 tag 里见过的写法）：

    * 前面的 ``v`` / ``V`` 去掉（GitHub 上两种写法都有）；
    * 段数不足三位用 0 补齐，``"2.1"`` 当成 ``(2,1,0)``；
    * 段数超过三位**截断**，``"2.0.6.1"`` 当成 ``(2,0,6)``——发布标签就是 x.y.z，
      多出来的是内部构建号，不该影响"谁更新"的判断；
    * 后缀直接丢：``"2.1.0-beta.2"`` 的前三段仍是 ``(2,1,0)``；
    * 有任何一段不含数字就当作解析失败，返回 ``()``（调用方据此认为"版本号不可比"）。

    返回元组而不是字符串，是为了能直接比大小（``(2,1,0) > (2,0,6)``）。
    """
    if text is None:
        return ()
    raw = str(text).strip()
    if not raw:
        return ()
    if raw[0] in "vV":
        raw = raw[1:].strip()
    if not raw:
        return ()

    parts = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk.strip():
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return ()
        parts.append(int(digits))

    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_latest_version(current, timeout=5.0):
    """问一次 GitHub，返回 ``{'ok', 'latest', 'url', 'newer', 'error'}``。

    各字段含义：

    * ``ok``：**这次检查本身**成功没有（网络通、响应是合法 JSON、里面有版本号）；
    * ``latest``：最新版本的裸版本号，如 ``"2.1.0"``（没有前面的 ``v``）；
    * ``url``：该版本的发布页；拿不到就退回项目主页；
    * ``newer``：最新版是否比 ``current`` 新；任一方版本号解析不出来时恒为 ``False``
      （宁可不说"有新版"，也别催着用户去装一个并不存在的东西）；
    * ``error``：失败原因，``ok`` 为真时是空串。

    **绝不抛异常。** 断网、502、公司代理拦截、GitHub 限流（403）、返回 HTML
    登录页……都只是把原因写进 ``error``。

    这是同步函数，跑起来最长会占住 ``timeout`` 秒；界面必须放到后台线程调，
    否则窗口会僵住——这不是本模块能替调用方决定的事。默认 5 秒是刻意的：
    检查更新只是"顺手看一眼"，卡住 5 秒就该放弃，不值得拖着界面走。
    """
    result = {"ok": False, "latest": "", "url": "", "newer": False, "error": ""}
    try:
        request = urllib.request.Request(
            LATEST_API,
            headers={
                # GitHub 的接口拒绝没有 User-Agent 的请求（403）
                "User-Agent": "MediaFireBatchDownloader/%s (+%s)" % (__version__, PROJECT_URL),
                "Accept": "application/vnd.github+json",
            },
        )
        response = urllib.request.urlopen(request, timeout=timeout)
        try:
            raw = response.read(512 * 1024)  # 正常的 release 响应只有几 KB，这里只是上个保险
        finally:
            try:
                response.close()
            except Exception:
                pass

        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            result["error"] = "接口返回的不是有效 JSON（可能被网络代理拦截）"
            return result
        if not isinstance(payload, dict):
            result["error"] = "接口返回的数据格式不对"
            return result

        tag = payload.get("tag_name") or payload.get("name") or ""
        latest = str(tag).strip()
        if latest[:1] in ("v", "V"):
            latest = latest[1:].strip()
        result["latest"] = latest
        result["url"] = payload.get("html_url") or PROJECT_URL
        if not latest:
            result["error"] = "接口响应里没有版本号"
            return result

        latest_info = parse_version(latest)
        current_info = parse_version(current)
        if not latest_info:
            result["error"] = "最新版本号无法解析：%s" % latest
            return result
        result["ok"] = True
        if current_info:
            result["newer"] = latest_info > current_info
        return result

    except urllib.error.HTTPError as exc:
        # 常见的是 403（未授权访问次数用尽）与 404（仓库改名/删除）
        result["error"] = "接口返回 HTTP %s（%s）" % (exc.code, exc.reason)
    except urllib.error.URLError as exc:
        result["error"] = "连不上更新服务器：%s" % (exc.reason,)
    except Exception as exc:  # noqa: BLE001 —— 检查更新失败不该影响任何功能
        result["error"] = "检查更新失败：%s" % (exc,)
    return result
