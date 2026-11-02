#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载引擎（2.0）：固定线程池、断点续传、SHA-256 校验、失败清单联动。

相对 5.0 的三处改动（都对应真实痛点）
-------------------------------------
1. **失败可追溯**：每次失败都写进 :class:`mfd.failures.FailureBook`，带上
   错误分类（403 / 超时 / 磁盘满……）与系统平台，界面上的「失败清单」页
   因此能"只列失败项、只重试失败项"，而不是让人翻日志找。
2. **只重试失败项**：``retry_failed()`` 把失败任务原地重置成排队态，重新
   喂给正在运行的线程池；线程池已收工则自动再起一轮。不碰已完成/已跳过的。
3. **队列可追加**：内部用 ``deque`` 而不是"单调下标"，这样运行中追加任务
   （重试、用户又粘了一批链接）能被正在干活的线程接住，不用整体重启。

其余行为沿用 5.0 的成熟实现：Range 续传、大小容差校验、官方 SHA-256 校验、
``.part`` 断点保留、原子改名、不覆盖用户已有文件。
"""

import collections
import hashlib
import os
import threading
import time
import traceback

from . import mediafire
from .failures import FailureBook, classify_failure
from .storage import History
from .util import (
    describe_write_error,
    fit_in_directory,
    human_size,
    human_time,
    pick_writable_fallback,
    probe_writable_dir,
    sanitize_filename,
)

CHUNK_SIZE = 1024 * 1024  # 1 MB
PART_SUFFIX = ".part"

# 任务状态
STATUS_WAITING = "waiting"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

STATUS_TEXT = {
    STATUS_WAITING: "排队中",
    STATUS_RUNNING: "下载中",
    STATUS_DONE: "已完成",
    STATUS_SKIPPED: "已存在，跳过",
    STATUS_FAILED: "失败",
    STATUS_CANCELED: "已取消",
}

# 终态：不会再被下载线程处理
TERMINAL_STATUS = (STATUS_DONE, STATUS_SKIPPED, STATUS_CANCELED)

# 队列空转时的等待窗口：线程取不到任务先等这么久，给"运行中追加任务"留缝
DRAIN_GRACE_SECONDS = 1.5


class DownloadTask(object):
    """一个待下载文件的全部状态。"""

    __slots__ = ("name", "url", "size", "quickkey", "folder", "hash_value",
                 "status", "error", "fail_kind", "saved_path", "downloaded",
                 "speed", "started_at", "finished_at", "attempts")

    def __init__(self, name, url, size=0, quickkey="", folder="", hash_value=""):
        self.name = sanitize_filename(name or "", "unknown_file")
        self.url = url
        try:
            self.size = int(size or 0)
        except (TypeError, ValueError):
            self.size = 0
        self.quickkey = quickkey or ""
        self.folder = folder or ""
        # MediaFire 接口给的 SHA-256（大写十六进制）。这是校验"文件真的下全了"
        # 最硬的证据：比只看大小可靠得多，也不会有取整误差导致的误判。
        self.hash_value = (hash_value or "").strip().upper()
        self.status = STATUS_WAITING
        self.error = ""
        self.fail_kind = ""      # 失败分类（failures.KIND_TEXT 的键），成功/排队时为空
        self.saved_path = ""
        self.downloaded = 0
        self.speed = 0.0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.attempts = 0

    @property
    def percent(self):
        if self.size > 0:
            return min(100.0, self.downloaded * 100.0 / self.size)
        return 0.0

    def eta_seconds(self):
        if self.status != STATUS_RUNNING or self.speed <= 1 or self.size <= 0:
            return None
        remain = max(0, self.size - self.downloaded)
        return remain / (self.speed * 1024.0)  # speed 单位是 KB/s

    def reset_for_retry(self):
        """把任务退回排队态（保留已下载字节与断点文件，续传接着下）。"""
        self.status = STATUS_WAITING
        self.error = ""
        self.fail_kind = ""
        self.speed = 0.0
        self.attempts = 0
        self.started_at = 0.0
        self.finished_at = 0.0

    def __repr__(self):
        return "<DownloadTask %s %s %d/%d>" % (self.name, self.status, self.downloaded, self.size)


def _read_chunk(response, size):
    """优先用 ``read1``：有多少给多少，不会为了凑满 size 而把进度假死在 99%。

    ``HTTPResponse.read(n)`` 会一直等到读满 n 字节或连接结束——服务器在文件末尾
    附近限速或挂起时，界面就会长时间停在 99% 不动（用户 2026-11-02 报的现象）。
    ``read1`` 收到多少立刻返回，数据能马上落盘、进度能平滑推进。
    """
    read1 = getattr(response, "read1", None)
    if read1 is None:
        return response.read(size)
    return read1(size)


class Downloader(object):
    """下载队列与工作线程池。

    ``on_event(event, payload)`` 是唯一的对外通知口（界面把它转成主线程刷新）：

      * ``'log'``       → 文本消息
      * ``'task'``      → 单个任务状态变化（payload 是 DownloadTask）
      * ``'progress'``  → 汇总进度（payload 是 dict，同 ``stats()``）
      * ``'failure'``   → 新增一条失败记录（payload 是 FailureRecord）
      * ``'finished'``  → 本轮全部任务结束（payload 是统计 dict）
    """

    def __init__(self, download_dir, history=None, max_threads=2, gap_seconds=1.0,
                 on_event=None, max_retries=3, timeout=(15, 30), failures=None):
        self.download_dir = download_dir
        self.history = history if history is not None else History()
        self.max_threads = max(1, min(int(max_threads or 1), 8))
        self.gap_seconds = max(0.0, float(gap_seconds or 0.0))
        self.max_retries = max(0, int(max_retries))
        self.timeout = timeout
        # http_get 只接受单个超时值。元组的第二项是"读超时"，传给它；
        # 第一项是"连接超时"，由 socket 层与 probe 使用。不接上的话这个参数
        # 就是个摆设——实际所有请求都会退回 mediafire.READ_TIMEOUT。
        if isinstance(timeout, (tuple, list)) and len(timeout) >= 2:
            self.read_timeout = timeout[1]
        else:
            self.read_timeout = timeout or 30
        self.on_event = on_event or (lambda event, payload: None)
        self.failures = failures if failures is not None else FailureBook()

        self.tasks = []
        self._lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._threads = []
        self._order = collections.deque()   # 待处理任务（FIFO，可运行中追加）
        self._active = 0
        self._total_downloaded = 0
        self._started_at = 0.0
        self._run_token = 0
        # 目录可写性探测结果缓存：{目录: (ok, reason)}，避免每个文件都写一次探测
        self._writable_cache = {}
        # 最近一次"启动失败"的原因，供界面直接显示（否则用户只会看到
        # "没有可下载的任务"，完全猜不到其实是保存目录写不进去）
        self.last_error = ""

    # ------------------------------------------------------------------
    # 对外状态
    # ------------------------------------------------------------------
    @property
    def is_running(self):
        return any(t.is_alive() for t in self._threads)

    @property
    def is_paused(self):
        return self._pause_flag.is_set()

    def stats(self):
        counts = {
            STATUS_WAITING: 0, STATUS_RUNNING: 0, STATUS_DONE: 0,
            STATUS_SKIPPED: 0, STATUS_FAILED: 0, STATUS_CANCELED: 0,
        }
        for task in self.tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        done_bytes = sum(t.downloaded for t in self.tasks)
        elapsed = (time.time() - self._started_at) if self._started_at else 0
        speed = (done_bytes / 1024.0 / elapsed) if elapsed > 0 else 0.0
        return {
            "counts": counts,
            "total": len(self.tasks),
            "finished": counts[STATUS_DONE] + counts[STATUS_SKIPPED] +
                        counts[STATUS_FAILED] + counts[STATUS_CANCELED],
            "bytes": done_bytes,
            "elapsed": elapsed,
            "speed": speed,
            "paused": self.is_paused,
            "failed_items": counts[STATUS_FAILED],
        }

    def failed_tasks(self):
        """当前失败的 DownloadTask 列表（不含历史清单里已经不在队列的）。"""
        return [t for t in self.tasks if t.status == STATUS_FAILED]

    def set_options(self, max_threads=None, gap_seconds=None):
        """运行前调整参数；已在运行时只影响后续新起的线程（不强杀现有线程）。"""
        if max_threads is not None:
            self.max_threads = max(1, min(int(max_threads or 1), 8))
        if gap_seconds is not None:
            self.gap_seconds = max(0.0, float(gap_seconds or 0.0))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def load_tasks(self, items, append=False):
        """把 ``mediafire.resolve_link()`` 的结果装进队列，并标记历史命中项。

        ``append=True`` 时追加到现有队列（用户又粘了一批链接），否则整体替换。
        """
        new_tasks = []
        skipped = 0
        duplicates = 0
        seen = set()
        if append:
            for task in self.tasks:
                seen.add((task.folder, task.name, task.size, task.quickkey))

        for item in items:
            task = DownloadTask(
                name=item.get("name", ""),
                url=item.get("url", ""),
                size=item.get("size", 0),
                quickkey=item.get("quickkey", ""),
                folder=item.get("folder", ""),
                hash_value=item.get("hash", ""),
            )
            # 同一次解析里出现完全相同的文件（同一个文件夹链接粘两遍，或同一
            # 文件夹被两条路径遍历到）只排一次队。
            identity = (task.folder, task.name, task.size, task.quickkey)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)

            with self._history_lock:
                already = self.history.has(task.name, task.size, task.quickkey)
            # 记录说下过，还要看文件是否真的还在盘上：用户可能已经把文件删了
            # （那就是想重下），这时不能跳过。
            # 早期写法是 `already and (... or task.size > 0)`，那个 `or` 把磁盘
            # 检查整个短路掉了——只要接口给了大小就一律跳过，用户删了文件也下不了。
            if already and self._file_already_on_disk(task):
                task.status = STATUS_SKIPPED
            new_tasks.append(task)

        if append:
            self.tasks = self.tasks + new_tasks
        else:
            self.tasks = new_tasks
            self._total_downloaded = 0

        for task in new_tasks:
            if task.status == STATUS_SKIPPED:
                skipped += 1
        message = ("又加入队列 %d 个文件" if append else "已加入队列 %d 个文件") % len(new_tasks)
        if duplicates:
            message += "（自动去掉 %d 个重复项）" % duplicates
        if skipped:
            message += "，其中 %d 个此前下载过、自动跳过" % skipped
        self._log(message + "。")
        return new_tasks

    def _file_already_on_disk(self, task):
        if not self.download_dir:
            return False
        path = os.path.join(self.download_dir, task.folder, task.name) if task.folder \
            else os.path.join(self.download_dir, task.name)
        try:
            if os.path.isfile(path):
                if task.size <= 0:
                    return True
                return os.path.getsize(path) == task.size
        except OSError:
            return False
        return False

    def set_download_dir(self, directory):
        """换保存目录。

        为什么需要这个方法：引擎是第一次解析时按当时的保存位置建好的，
        之后用户在输入框里改成别的文件夹，如果只改界面文本、不通知引擎，
        引擎还会拿老目录去判定权限——表现就是「我明明换了地方，它还说
        没有写入权限」。所以每次开始前都要同步一次。
        """
        directory = (directory or "").strip()
        if not directory or directory == self.download_dir:
            return False
        self.download_dir = directory
        self._writable_cache.clear()
        self.last_error = ""
        self._log("保存位置已切换到：%s" % directory)
        return True

    def start(self):
        if self.is_running:
            self._log("任务已在运行中。")
            return False
        pending = [t for t in self.tasks if t.status == STATUS_WAITING]
        if not self.tasks:
            self._log("队列为空，没有需要下载的文件。")
            return False
        if not pending:
            # 注意：这里**不能**发 finished 事件。start() 没跑起来，UI 收到
            # finished 会以为"下载正常结束"，弹一个"完成 0 · 失败 0"的框，
            # 而真实原因是"根本没有可跑的任务"——两者混在一起就是 2.0.2
            # 那个「点开始啥也不干、只弹下载结束」的 bug。
            self.last_error = ("没有待下载的文件：队列里的 %d 个文件都已完成、已跳过或已失败。"
                               % len(self.tasks))
            self._log("没有待下载的文件（全部已完成、已跳过或已失败，可点「重试全部失败」）。")
            return False

        # 开始前先确认保存目录真的能写（makedirs 在"目录已存在但只读"时不报错，
        # 结果要等到 open(.part) 才炸，错误信息里只有一串路径，用户无从下手）
        self.last_error = ""
        writable, reason = probe_writable_dir(self.download_dir)
        if not writable:
            self._writable_cache[self.download_dir] = (False, reason)
            # 写不进去不要硬停。用户的目录可能因为 ACL、OneDrive 占位、杀软
            # 锁定、移动硬盘掉线而"看起来正常却写不进"，这些他都没法一眼看出。
            # 所以：换一个确定能写的地方继续跑，并明确告诉他换了哪、为什么换。
            fallback = pick_writable_fallback()
            if not fallback:
                self.last_error = "保存目录无法写入：%s\n%s" % (reason, self.download_dir)
                self._log("保存目录无法写入：%s" % reason)
                self._log("    目录：%s" % self.download_dir)
                self._log("    也没找到可替代的可写目录，请手动换一个文件夹再开始。")
                return False
            self._log("WARN 保存目录写不进去（%s）：%s" % (reason, self.download_dir))
            self._log("    已自动改用：%s" % fallback)
            self._log("    想换回自己的目录：在界面上改好后直接点「开始下载」。")
            self.download_dir = fallback
            writable = True


        self._stop_flag.clear()
        self._pause_flag.clear()
        self._started_at = time.time()
        self._active = 0
        self._run_token += 1

        # 队列重建：只放排队态任务，保证"已完成/已跳过"不会被重新下载
        self._order = collections.deque(pending)

        workers = max(1, min(self.max_threads, len(pending)))
        self._threads = []
        for index in range(workers):
            thread = threading.Thread(
                target=self._worker, args=(index,), name="mfd-worker-%d" % index, daemon=True
            )
            self._threads.append(thread)
            thread.start()
        self._log("开始下载：%d 个文件，%d 个线程，间隔 %.1f 秒。"
                  % (len(pending), workers, self.gap_seconds))

        monitor = threading.Thread(target=self._monitor, daemon=True, name="mfd-monitor")
        monitor.start()
        return True

    def retry_failed(self):
        """只重试失败项，返回被重新排队的任务数。

        为什么不重下全部：几百个文件里失败的可能只有十来个，重下全部既浪费流量
        又要重新面对"哪些是已存在"的判定；用户要的就是"点一下，只补失败的那几个"。
        """
        failed = self.failed_tasks()
        if not failed:
            self._log("没有失败的下载项，无需重试。")
            return 0

        for task in failed:
            task.reset_for_retry()

        if self.is_running:
            # 线程池还在跑：直接把任务追加进队列，正在等任务的线程会接住
            for task in failed:
                self._order.append(task)
            self._log("已把 %d 个失败项重新排队。" % len(failed))
            self._emit("progress", self.stats())
        else:
            self._log("正在重试 %d 个失败项……" % len(failed))
            self.start()
        return len(failed)

    def retry_task(self, task):
        """重试单个任务。"""
        if task is None or task.status != STATUS_FAILED:
            return False
        task.reset_for_retry()
        if self.is_running:
            self._order.append(task)
            self._emit("progress", self.stats())
        else:
            self.start()
        return True

    def stop(self):
        """立即停止：置停止标志，正在传输的线程在下一个数据块退出并保留 ``.part``。"""
        self._stop_flag.set()
        self._pause_flag.clear()
        self._log("已发出停止指令，正在收尾……")

    def stop_and_wait(self, timeout=5.0):
        """停止并等线程收尾（关窗口时用）。

        为什么必须等：每个下载线程都在 ``.part`` 上写数据。用户直接关窗口、进程
        立刻退出，正在写的那一块可能只写了一半。虽然它仍能续传（按文件实际大小
        接着下），但等线程收尾能把这个"最坏情况"直接消掉。
        """
        self.stop()
        deadline = time.time() + max(0.0, timeout)
        for thread in list(self._threads):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        left = [t for t in self._threads if t.is_alive()]
        if left:
            self._log("有 %d 个下载线程还没停下（已等待 %.0f 秒），本次进度已保留在 .part 文件里。"
                      % (len(left), timeout))
        return not left

    def pause(self):
        self._pause_flag.set()
        self._log("已暂停：正在传输的文件会先停下，未开始的保持排队。")

    def resume(self):
        if self._pause_flag.is_set():
            self._pause_flag.clear()
            self._log("已继续。")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _log(self, message):
        self._emit("log", message)

    def _emit(self, event, payload):
        try:
            self.on_event(event, payload)
        except Exception:
            # 回调（界面）出错不能反过来搞死下载线程
            traceback.print_exc()

    def _next_task(self):
        with self._lock:
            while True:
                try:
                    task = self._order.popleft()
                except IndexError:
                    return None
                if task.status == STATUS_WAITING:
                    return task

    def _worker(self, index):
        while not self._stop_flag.is_set():
            self._wait_if_paused()
            if self._stop_flag.is_set():
                break
            task = self._next_task()
            if task is None:
                # 队列暂时空：给"运行中追加任务"留一个窗口，真没活才收工
                if self._drain_grace():
                    continue
                break
            try:
                self._run_task(task)
            except Exception as exc:  # 兜底：任何未预期异常都不能杀死工作线程
                task.status = STATUS_FAILED
                task.error = "内部错误：%s" % exc
                task.fail_kind = "unknown"
                self._log("【内部错误】%s —— %s" % (task.name, exc))
                self._record_failure(task, exc=exc, message=task.error,
                                     detail=traceback.format_exc())
                self._emit("task", task)
            if self.gap_seconds > 0 and not self._stop_flag.is_set():
                self._interruptible_sleep(self.gap_seconds)

    def _drain_grace(self):
        """队列空时短暂等待新任务；返回 True 表示等到了。"""
        deadline = time.time() + DRAIN_GRACE_SECONDS
        while time.time() < deadline:
            if self._stop_flag.is_set():
                return False
            if self._order:
                return True
            time.sleep(0.05)
        return bool(self._order)

    def _wait_if_paused(self):
        while self._pause_flag.is_set() and not self._stop_flag.is_set():
            time.sleep(0.2)

    def _monitor(self):
        """周期性汇报汇总进度，直到所有工作线程退出。"""
        while True:
            alive = [t for t in self._threads if t.is_alive()]
            self._emit("progress", self.stats())
            if not alive:
                break
            time.sleep(0.5)
        self._emit("progress", self.stats())
        self._emit("finished", self.stats())

    def _run_task(self, task):
        task.status = STATUS_RUNNING
        task.fail_kind = ""
        task.started_at = time.time()
        task.downloaded = 0
        task.error = ""
        self._emit("task", task)

        target_dir = self.download_dir
        if task.folder:
            # 子文件夹结构按 MediaFire 里的层级还原；目录名已净化过
            target_dir = os.path.join(self.download_dir, *[
                sanitize_filename(part, "subfolder")
                for part in task.folder.split("/") if part
            ])
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                self._fail(task, "无法创建目录 %s：%s" % (target_dir, exc),
                           detail=repr(exc), exc=exc)
                return

        # 子目录也要确认能写：makedirs 对"已存在但只读"的目录不报错，
        # 否则要等到 open(.part) 才炸，报出来的只有一串路径
        cached = self._writable_cache.get(target_dir)
        if cached is None:
            cached = probe_writable_dir(target_dir)
            self._writable_cache[target_dir] = cached
        if not cached[0]:
            self._fail(task, "保存位置无法写入：%s" % cached[1],
                       detail="目录：%s" % target_dir)
            # 目录不可写时后面每个文件都会以同样理由失败，继续跑只会刷满失败清单
            self.last_error = "保存位置无法写入：%s\n%s" % (cached[1], target_dir)
            self._log("保存目录不可写，已停止队列。换一个文件夹后点「重试全部失败」即可。")
            self._stop_flag.set()
            return

        final_path, final_name = fit_in_directory(target_dir, task.name)
        part_path = final_path + PART_SUFFIX

        # 目录里已有同名同大小的文件 → 直接视为完成，不再重复下载
        if os.path.isfile(final_path):
            try:
                existing = os.path.getsize(final_path)
            except OSError:
                existing = -1
            if task.size <= 0 or existing == task.size:
                task.status = STATUS_SKIPPED
                task.saved_path = final_path
                task.downloaded = existing if existing > 0 else 0
                self._remember(task)
                self._forget_failure(task)
                self._log("已存在同大小文件，跳过：%s" % final_name)
                self._emit("task", task)
                return
            # 同名但大小不同：改名保存，绝不覆盖用户已有文件
            final_path, final_name = self._avoid_overwrite(target_dir, final_name)
            part_path = final_path + PART_SUFFIX

        ok = self._download_with_retry(task, final_path, part_path)
        if ok:
            task.saved_path = final_path
            task.finished_at = time.time()
            self._remember(task)
            self._forget_failure(task)
            took = human_time(task.finished_at - task.started_at)
            self._log("完成：%s（%s，用时 %s）"
                      % (final_name, human_size(task.size or task.downloaded), took))
        self._emit("task", task)

    def _download_with_retry(self, task, final_path, part_path):
        last_error = ""
        last_detail = ""
        last_exc = None
        for attempt in range(1, self.max_retries + 2):  # 首次 + max_retries 次重试
            if self._stop_flag.is_set():
                task.status = STATUS_CANCELED
                task.error = "用户停止，已保留断点文件"
                return False
            task.attempts = attempt
            if attempt > 1:
                self._log("第 %d 次尝试：%s" % (attempt, task.name))
            try:
                self._download_once(task, final_path, part_path)
                return True
            except _Canceled:
                task.status = STATUS_CANCELED
                task.error = "用户停止，已保留断点文件"
                self._log("已停止：%s（已下载部分保留为 .part，下次自动续传）" % task.name)
                return False
            except mediafire.MediaFireError as exc:
                last_error = exc.user_message
                last_detail = exc.detail
                last_exc = exc
            except OSError as exc:
                last_error = describe_write_error(part_path, exc)
                last_detail = "OSError: %r" % (exc,)
                last_exc = exc
            except Exception as exc:
                last_error = "未预期错误：%s" % exc
                last_detail = traceback.format_exc()
                last_exc = exc

            if self._stop_flag.is_set():
                task.status = STATUS_CANCELED
                task.error = "用户停止，已保留断点文件"
                return False
            if attempt <= self.max_retries:
                wait = min(2 ** attempt, 30)
                self._log("失败（%s），%d 秒后重试 —— %s" % (last_error, wait, task.name))
                if last_detail:
                    self._log("    技术细节：%s" % str(last_detail).replace("\n", " ")[:300])
                if self._interruptible_sleep(wait):
                    task.status = STATUS_CANCELED
                    task.error = "用户停止，已保留断点文件"
                    return False
        self._fail(task, last_error, detail=last_detail, exc=last_exc, part_path=part_path)
        return False

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_flag.is_set():
                return True
            time.sleep(0.2)
        return False

    def _fail(self, task, message, detail="", exc=None, part_path=""):
        task.status = STATUS_FAILED
        task.error = message
        kind, _hint = classify_failure(exc, message, detail)
        task.fail_kind = kind
        self._log("失败：%s —— %s" % (task.name, message))
        if detail:
            self._log("    技术细节：%s" % str(detail).replace("\n", " ")[:300])
        self._record_failure(task, exc=exc, kind=kind, message=message,
                             detail=detail, part_path=part_path)

    def _record_failure(self, task, exc=None, kind=None, message="", detail="", part_path=""):
        """把失败写进清单并发事件；清单本身出错不能影响下载线程。"""
        try:
            record = self.failures.add_task(
                task, exc=exc, kind=kind, message=message, detail=detail, part_path=part_path
            )
        except Exception as exc2:
            self._log("注意：失败记录写入失败（不影响其它任务）：%s" % exc2)
            return
        if record is not None:
            self._emit("failure", record)

    def _forget_failure(self, task):
        """下载成功后把同名失败记录移出清单。"""
        try:
            self.failures.discard(task.quickkey, task.name, task.size)
        except Exception:
            pass

    def _avoid_overwrite(self, directory, filename):
        """同目录存在不同大小的同名文件时，改用 ``名字 (2).ext``。"""
        stem, ext = os.path.splitext(filename)
        for index in range(2, 100):
            candidate = "%s (%d)%s" % (stem, index, ext)
            path, candidate = fit_in_directory(directory, candidate)
            if not os.path.exists(path):
                return path, candidate
        fallback = "%s_%d%s" % (stem, int(time.time()), ext)
        return fit_in_directory(directory, fallback)

    def _download_once(self, task, final_path, part_path):
        """单次下载尝试：解析直链 → （必要时）续传 → 校验大小与哈希 → 落盘。"""
        file_page = task.url
        direct_url, page_size = mediafire.resolve_download_url(file_page)
        # 注意：接口给的大小是**权威值**（实测与服务器 Content-Range 完全一致），
        # 而文件页按钮上的"48.5 MB"是四舍五入过的，所以只在接口没给大小时才采用它。
        # 这正是"明明下完了却报大小不符"的根因。
        if page_size and not task.size:
            task.size = page_size
            self._log("接口未提供大小，改用文件页显示的大小：%s（%s）"
                      % (task.name, human_size(page_size)))
            self._emit("task", task)

        resume_from = 0
        if os.path.isfile(part_path):
            try:
                resume_from = os.path.getsize(part_path)
            except OSError:
                resume_from = 0
        if task.size and resume_from >= task.size:
            # 断点文件已经完整（上次在改名阶段被打断）
            os.replace(part_path, final_path)
            task.downloaded = resume_from
            task.status = STATUS_DONE
            return
        if resume_from and task.size and resume_from > task.size:
            resume_from = 0  # 断点比远端还大，说明源文件变了，重下

        headers = {}
        if resume_from:
            headers["Range"] = "bytes=%d-" % resume_from
            self._log("从断点继续：%s（已有 %s）" % (task.name, human_size(resume_from)))

        response, _ = mediafire.http_get(direct_url, headers=headers, stream=True,
                                         timeout=self.read_timeout)

        try:
            status = getattr(response, "status", 200)
            content_length = int(response.headers.get("Content-Length") or 0)
            content_range = response.headers.get("Content-Range") or ""
            if resume_from and status != 206:
                # 服务器忽略了 Range：删除断点重下，避免拼出损坏文件
                self._log("服务器不支持续传，改为重新下载：%s" % task.name)
                resume_from = 0

            if content_range:
                total = int(content_range.split("/")[-1] or 0) or \
                    (resume_from + content_length)
            else:
                total = resume_from + content_length
            if total and not task.size:
                task.size = total
                self._emit("task", task)
            elif total and task.size and abs(total - task.size) > max(4096, task.size // 100):
                # 差异超过 1% 才认远端的新值，免得每次重试都把总量改来改去
                self._log("远端总长与记录不一致（%s → %s），以远端为准：%s"
                          % (human_size(task.size), human_size(total), task.name))
                task.size = total
                self._emit("task", task)

            mode = "ab" if resume_from else "wb"
            task.downloaded = resume_from
            downloaded_this_run = 0
            start = time.time()
            last_report = start

            with open(part_path, mode) as handle:
                while True:
                    if self._stop_flag.is_set():
                        raise _Canceled()
                    self._wait_if_paused()
                    chunk = _read_chunk(response, CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    task.downloaded += len(chunk)
                    downloaded_this_run += len(chunk)
                    now = time.time()
                    if now - last_report >= 0.5:
                        elapsed = now - start
                        task.speed = (downloaded_this_run / 1024.0 / elapsed) if elapsed > 0 else 0.0
                        last_report = now
                        self._emit("task", task)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        finally:
            try:
                response.close()
            except Exception:
                pass

        # 大小校验：远端给了总长就比对，防止半截文件被当成完成。
        # 这里不能"绝对相等"：文件页按钮上写的是四舍五入过的整数 MB
        # （实测 53371597 字节显示成 "48.5 MB"，差 4.6%），只有接口拿不到大小时
        # 才会用到它。为了既拦住"半截文件"，又不误杀这种取整误差，留一点余量。
        actual = os.path.getsize(part_path)
        allowed = max(4096, int(task.size * 0.01)) if task.size else 0
        if task.size and abs(actual - task.size) > allowed:
            raise mediafire.MediaFireError(
                "下载不完整：已收到 %s，应为 %s（再运行一次本工具会自动续传）"
                % (human_size(actual), human_size(task.size)),
                "size mismatch on %s" % final_path,
            )

        # 官方 SHA-256 校验：MediaFire 文件夹接口每个文件都带 hash 字段，
        # 这是比"大小对不对"硬得多的证据（大小只说明长度，哈希能证明内容一致）。
        # 相符才落盘；不符按失败处理，下次运行会重新下载而不是留下一个坏文件。
        if task.hash_value:
            digest = self._sha256_of(part_path)
            if digest != task.hash_value:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
                raise mediafire.MediaFireError(
                    "文件校验不通过，已删除并会重新下载：%s" % task.name,
                    "sha256 mismatch: got %s want %s" % (digest, task.hash_value),
                )
            self._log("校验通过（SHA-256）：%s" % task.name)

        # 原子改名：先删目标再 rename，避免 Windows 上 rename 到已存在路径失败
        if os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError as exc:
                raise mediafire.MediaFireError(
                    "无法覆盖同名文件 %s：%s" % (os.path.basename(final_path), exc),
                    "remove failed: %r" % (exc,),
                )
        os.replace(part_path, final_path)
        task.status = STATUS_DONE
        task.speed = 0.0
        task.downloaded = task.size or actual

    @staticmethod
    def _sha256_of(path):
        """算文件的 SHA-256（分块读，避免把大文件整个吃进内存）。"""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest().upper()

    def _remember(self, task):
        with self._history_lock:
            try:
                self.history.add(
                    name=task.name,
                    size=task.size or task.downloaded,
                    quickkey=task.quickkey,
                    folder_key=task.folder,
                    saved_path=task.saved_path,
                )
                self.history.save()
            except Exception as exc:
                # 记录写不进去不能影响"文件已经下载成功"这个事实，
                # 但必须如实告诉用户，不能静默。
                self._log("注意：下载记录写入失败（文件本身不受影响）：%s" % exc)


class _Canceled(Exception):
    """内部：用户停止导致的提前退出。"""
