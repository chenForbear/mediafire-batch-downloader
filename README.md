# MediaFire Batch Downloader 2.0

> A small Windows desktop tool that turns MediaFire share links — a single file, or a whole folder — into a **parsed, queued, resumable** batch download, with a failure list you can actually act on. No installer, no Python required, no account, nothing uploaded anywhere.

> [!IMPORTANT]
> **Unofficial project.** Not affiliated with, endorsed by, or connected to MediaFire in any way. This is a third-party client that resolves **publicly shared links** and downloads them — it does **not** bypass login, paywalls, password-protected shares, or any other access control, contains no DRM circumvention, and has no "crack / speed-unlock / throttle-bypass" functionality.
>
> **Use responsibly.** Only download content you have the right to obtain. The tool is a neutral file-transfer client; what you download and whether that infringes anyone's copyright is up to you, not the project.

![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)
![Standard Library](https://img.shields.io/badge/dependencies-none-4B5563)
![Platform](https://img.shields.io/badge/platform-Windows%207%20SP1%20%E2%80%93%2011-0078D4)
![License](https://img.shields.io/badge/license-MIT-22C55E)

| | |
|---|---|
| Version | `2.0.0` (2026-11-01) |
| Platforms | Windows 7 SP1 / 8.1 / 10 / 11 |
| Runtime | Python 3.8+ (the packaged .exe needs none) |
| Third-party dependencies | **None.** Tkinter for the UI, standard library everywhere else |
| License | MIT |

> On version numbers: `2.0.0` is the start of a new numbering. The old `5.0` (and the earlier 4.x/3.x line) is **retired** — it shares no lineage with 2.0, and a bigger number here does not mean a bigger feature set. See [CHANGELOG.md](CHANGELOG.md).

---

## Contents

- [What it does](#what-it-does)
- [Screenshot](#screenshot)
- [Getting started](#getting-started)
- [Usage](#usage)
- [FAQ](#faq)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Development and tests](#development-and-tests)
- [Building the exe](#building-the-exe)
- [Known limits](#known-limits)
- [License](#license)
- [Credits](#credits)

---

## What it does

A MediaFire folder page lets you click one file at a time. Two hundred files means two hundred clicks — plus the usual mess: connections drop halfway, you end up with a truncated file and no idea which of the two hundred failed.

This tool does that whole loop in one pass:

1. **Paste once, expand everything** — folder links are walked recursively (subfolders optional), pagination is followed to the end, and single-file links plus `?key=` short links are recognised automatically;
2. **Queue without flooding the other side** — 1–8 worker threads, a configurable gap between files, exponential backoff on retries;
3. **Resume instead of restart** — files are written to `.part` first and continue from the known offset;
4. **Verify before calling it done** — SHA-256 recomputed locally against the value MediaFire reports; a mismatch is a failure, not a "completed" file;
5. **Keep a ledger of failures** — 21 error categories land in a failure list you can retry wholesale, copy as links, or export to JSON / Markdown;
6. **Hand over evidence** — the diagnostics tab cross-tabulates environment and "OS × error type", one click to export JSON for a bug report.

### Features

| Capability | What it actually does | Where |
|---|---|---|
| Link parsing | Folder / single file / `?key=` short links; breadth-first recursion, 100 entries per page; the folder listing is fetched first so the second request only happens when subfolders exist (≈7 s for a typical folder) | `mfd/mediafire.py` |
| Queueing | 1–8 threads (default 2), 1 s gap by default; deduplicated by quickkey or name+size | `mfd/downloader.py` |
| Resume | `.part` file + `Range: bytes=N-`; if the server ignores the range and sends the whole body, the partial data is discarded rather than stitched | `mfd/downloader.py` |
| Verification | SHA-256 against the API value; size tolerance `max(4096, 1%)`, anything beyond that is "incomplete download" | `mfd/downloader.py` |
| Failure list | 21 error categories; repeated failures bump a counter instead of piling up; grouped by kind and by platform; export JSON, Markdown, or a plain list of failed links | `mfd/failures.py` |
| Retry failures only | "Retry all failed" re-queues just the failed tasks — completed files are never touched again | `mfd/downloader.py` |
| Download history | Registry `HKCU\Software\MediaFireDownloader` by default, falling back to a JSON file; capped at 5000 entries; stores name, size, quickkey and folder name — never the full direct URL or absolute local path | `mfd/storage.py` |
| Diagnostics report | Environment snapshot plus an OS × error-type cross table, exported as JSON | `mfd/diagnostics.py` |
| UI | Dark / light themes; Queue / Failures / Log / Diagnostics tabs; overview pills; DPI aware; fonts probed rather than assumed | `mfd/ui/` |
| Close behaviour | While downloading, closing the window asks: keep running in background / stop and exit / cancel | `mfd/ui/app.py` |
| Filename safety | Illegal characters and reserved names handled, 180-char filename cap, 255-char full-path cap, same-name-different-size saved as `name (2).ext`, never overwritten | `mfd/util.py` |

---

## Screenshot

> Screenshot **to be added**. Run `pythonw run.pyw` (or the packaged exe) to see the real interface: dark / light themes with a one-click switch, and four tabs (Queue / Failures / Log / Diagnostics).
> This paragraph will be replaced once the screenshot is in.

---

## Getting started

### Option 1: grab the portable package (recommended)

1. Download `MediaFire-Downloader-2.0.1-portable-win64.zip` from [Releases](https://github.com/chenForbear/mediafire-batch-downloader/releases);
2. **Extract the whole archive first** — do not run anything from inside the zip viewer;
3. Double-click `首次使用-双击我.bat` in the extracted folder. It clears the "downloaded from the internet" mark on these files, creates a desktop shortcut, and launches the app. **One time only**;
4. From then on, use the desktop shortcut. No administrator rights, no Python.

> **About "Windows protected your PC"**: that is SmartScreen's default reaction to *any* unsigned binary — every independently built exe gets stopped once on first download. Step 3 removes the mark, so it will not appear again afterwards. If it still does, click "More info" → "Run anyway"; that is a one-off.
>
> **Why the single-file build is no longer the default**: onefile builds unpack themselves into `%TEMP%\_MEIxxxx` on every launch. When `%TEMP%` is not writable, is restricted by group policy, or is blocked by security software, double-clicking fails outright — while "Run as administrator" happens to succeed because it runs at a different integrity level, which makes it *look* like the program needs admin rights. The onedir build unpacks nothing, never touches `%TEMP%`, and starts far faster.
>
> On Windows 7, read the Win7 section of [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) first (SP1 and the UCRT update KB2999226 are hard requirements).

### Option 1b: single-file exe

Releases also ship `MediaFire-Downloader-2.0.1-windows-x64.exe`. One file, drop it anywhere — but it self-extracts on every launch (a few seconds slower) and needs a writable `%TEMP%`. Use it only when "one file on a USB stick" really matters.

### Option 2: run from source

```powershell
git clone https://github.com/chenForbear/mediafire-batch-downloader.git
cd mediafire-batch-downloader
python run.pyw
```

Python 3.8 or newer with Tkinter (the official Windows installer includes it). `run.pyw` is a windowed entry point, so **no console flashes up**. To check your environment, run the offline suite:

```powershell
python tests\test_offline.py
```

If anything does crash, the traceback lands in `%LOCALAPPDATA%\MediaFireDownloader2\crash.log`.

---

## Usage

Five steps, laid out in the same order in the UI.

### 1. Parse: links become tasks

Paste a MediaFire link into the input box — `Ctrl+V` works directly, or press `Ctrl+Enter` inside the box to paste and parse in one go. Three shapes are supported:

```
https://www.mediafire.com/folder/xxxxxxxxxxxx/folder-name
https://www.mediafire.com/file/xxxxxxxxxxxx/file-name.rar/file
https://www.mediafire.com/?key=xxxxxxxxxxxx
```

Options, all of them before parsing:

| Option | Meaning |
|---|---|
| Save to | Target directory. Defaults to the first available of Desktop → Documents → Downloads, with a `MediaFireDownloads` subfolder created underneath; "Browse…" to change |
| Threads | How many files download at once, 1–8, default 2. Too many invites rate limiting (see 429) |
| Gap (s) | Pause between tasks, 0.2–10 s, default 1.0. Raise it if you hit 429 |
| Include subfolders | Whether folder links recurse into subfolders, default on |

Hit **Parse and add to queue**. Parsing runs on a background thread, so the UI stays responsive and reports progress (which page, how many found) as it goes.

### 2. Queue: see what you're getting

The queue table has six columns: **file name / size / progress / speed / status / save location**.

- Six statuses only: `Queued`, `Downloading`, `Completed`, `Exists, skipped`, `Failed`, `Cancelled`;
- Entries already in the history whose local file is still present are marked `Exists, skipped` and not fetched again;
- Same name **and** same size in the target folder: skipped. Same name, **different** size: downloaded as `name (2).ext` — your existing file is never touched.

The overview pills at the top track **completed / failed / skipped / total** plus an ETA.

Three buttons: **Start**, **Pause** (becomes Resume), **Stop**.

### 3. Failures: know exactly what went wrong

Failures are recorded, not buried in a log: **file name / error type / reason / attempts / time**.

Error types are a fixed vocabulary of 21 categories — `HTTP 403 forbidden`, `Connection timed out`, `Out of disk space`, `Checksum mismatch`, `MediaFire API error`, and so on. Each carries a one-line remedy in plain language, so you are not left decoding `errno 13`.

Toolbar:

- **Retry all failed** — re-queue them (appended to a running download, or started immediately when idle);
- **Copy failed links** — the failed URLs go to the clipboard in one shot;
- **Export JSON** / **Export Markdown** — for records, for pasting into an issue, for sending to someone else;
- **Clear** — behind a confirmation prompt.

`F5` refreshes the list.

### 4. Retry: only what needs retrying

"Retry all failed" touches failed items only. Completed files are not re-requested and not overwritten. If you would rather download the failures somewhere else, "Copy failed links" gives you a plain list of URLs.

> A common combination: when a whole page of 403s appears, don't hammer retry before reading [the three causes of 403](#faq) — if the direct link simply expired, a retry re-resolves it and succeeds; if the file has been made private, a hundred retries change nothing.

### 5. Diagnostics: hand over the scene of the crime

The diagnostics tab does two things:

1. **List the environment**: OS and build, locale, UI fonts, DPI, current theme, whether the download directory is writable;
2. **Cross-tabulate failures**: a table of **OS × error type** — which category dominates on which system becomes obvious at a glance.

"Export diagnostics report" writes a JSON file; "Recollect" refreshes it. When you file a bug, **attach that file** (see the [issue template](.github/ISSUE_TEMPLATE/bug_report.md)).

Filenames can contain personal information — give the file a look before sharing. Keep the category labels (error type, platform): those are the part that actually localises the problem.

---

## FAQ

### Download fails with 403 — is the file gone, or is it me?

403 in this context has **three different causes**, and the remedies differ:

| Cause | How to tell | What to do |
|---|---|---|
| ① The direct link expired | The link was resolved long before the download started, or the queue sat idle for a while | Just retry. Every download attempt **re-resolves** the link first, so a retry normally succeeds |
| ② The file was made private | Other files in the same folder download fine, only this one 403s; or the page asks for a password / reports the file removed | Ask the sharer. This tool has no login state and cannot fetch private files |
| ③ ISP or proxy interception | Switching networks (phone hotspot, for instance) fixes it; happens consistently while a proxy is running | Disable the proxy or route `mediafire.com` direct. Corporate networks sometimes block the whole domain |

There is a lookalike worth knowing: HTTP 407 (proxy authentication required). It is classified separately as "proxy authentication", not folded into 403.

### What does "the direct link expired" mean?

The download button on a MediaFire share page points at a **time-limited** direct URL. Resolve it, leave it sitting, and it eventually stops working — that is where a 403 comes from.

This program handles it by design: it **re-resolves the link before every download attempt** rather than caching a URL once and reusing it. As long as the file itself still exists, resuming and retrying will not fail because the link got old.

### Can it download private files?

No. A private file requires a logged-in MediaFire session before the page will render a download button; this program stores and sends no account information at all, so there is nothing to work with.

A common misread: only *some* files inside a folder are private. The symptom is a task list shorter than what the web page shows, or a few files that always 403.

### Downloads stall whenever a proxy or VPN is on?

Two typical symptoms:

- **407**: the proxy wants credentials the program does not have → classified as "proxy authentication". Allow `mediafire.com` / `download*.mediafire.com` through, or go direct;
- **SSL certificate error**: some proxies perform TLS interception and the chain does not validate → classified as "certificate / TLS". The program uses the system trust store and enforces TLS 1.2+, and will not disable verification just to make a connection work.

### Does it run on Windows 7?

Yes, subject to three hard requirements (details and remedies in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)):

1. **SP1** is mandatory;
2. The **TLS 1.2** update (KB3140245 and friends) must be installed, otherwise every `https://` request fails;
3. A packaged build must ship **Tk 8.6 plus the tcl/tk data directories**, and must **strip the build machine's UCRT** (otherwise `api-ms-win-crt-*` is missing on Win7 and the app fails to start). `packaging/build.spec` already does both.

### I closed the window / lost the network halfway. Start over?

No. Bytes already written stay in `name.part` and the next attempt continues from there. Two cases restart honestly from zero: the server does not support ranges (returns the whole body instead of a 206), or the `.part` size is clearly wrong — a restart beats a corrupted file.

### Will it overwrite files with the same name?

Never. Same size → treated as present, skipped. Different size → saved as `name (2).ext`.

### Does it send my data anywhere?

Apart from MediaFire's own hosts (`www.mediafire.com`, `download*.mediafire.com`), the program contacts nothing. No analytics, no telemetry, no auto-update.

### Double-clicking does nothing / the window flashes and disappears

For the packaged build, check whether security software quarantined it. For the source build, read `%LOCALAPPDATA%\MediaFireDownloader2\crash.log`. On Windows 7, "double-click does nothing" is almost always a missing UCRT or Tk data directories that were not packaged.

---

## How it works

### Two phases

**Phase 1 — resolve (talking to MediaFire)**

This uses MediaFire's public web API (`https://www.mediafire.com/api/1.4`), not a private endpoint:

1. Extract the `folder_key` / `quick_key` from the link — `/folder/`, `/file/`, `?key=` and `folder_key=` are all recognised;
2. Folders go through `folder/get_content`, 100 entries per page, following `more_chunks` until it stops saying `yes`;
3. **The file listing is requested first**, and `folder_count` decides whether a second `content_type=folders` request is worth making; only then does breadth-first recursion begin (with a `visited` set, so a cycle cannot hang it). This cut a typical single-folder parse from 11.5 s to about 7 s;
4. When the API returns `result != "Success"`, the code is translated into plain language — `127` folder not found, `128` private, `129` invalid key or not a folder;
5. Single files go through `file/get_info` for name, size, quickkey and **SHA-256**;
6. Immediately before downloading, the share page is opened again and the **fresh** direct URL is taken from the `downloadButton` href (both HTML orderings are handled); the button text (`download (12.3 MB)`) doubles as a size fallback.

**Phase 2 — download (talking to disk)**

1. The target path is built from directory plus filename: subdirectories are created per folder segment (each segment sanitised), then `fit_in_directory` keeps the full path under 255 characters;
2. An existing `.part` means a `Range: bytes=N-` request; a response that is not `206 Partial Content` discards the partial data and restarts;
3. Data is written in 1 MB blocks with `fsync`, while progress is reported at most twice a second — fast enough to feel live, slow enough not to flood the UI;
4. Two checks on completion: size (tolerance `max(4096, 1%)`) and SHA-256;
5. Only then is the file renamed into place with `os.replace` — **at no point does a half-written file carry its final name**;
6. A failure is recorded in the failure list; a success discards the matching entry and appends to the download history.

### Threading model

- Worker pool `mfd-worker-N` (daemon threads), size clamped to 1–8;
- One monitor thread that aggregates progress and emits events every 0.5 s;
- Tasks live in a `deque`, with a 1.5 s drain window so tasks can be appended mid-run;
- The UI only consumes events (`log` / `task` / `progress` / `failure` / `finished`) and mutates widgets on the main thread — background threads never touch a widget.

### Resume vs. verification

These solve two different problems and are deliberately separate:

- **Resume solves time** — an hour-long download should not restart because the connection blipped. That is `.part` plus `Range`;
- **Verification solves correctness** — resuming implicitly assumes "the bytes I stitched together really are the original file". MediaFire reports a SHA-256, so it is recomputed locally and compared; a mismatch deletes the `.part` and fails the task. **A checksum failure is never counted as success** — that matters far more than download speed.

Retry counts, timeouts and backoff are constants at the top of `mfd/mediafire.py` and `mfd/downloader.py`: a request times out after 30 s (the remote-size probe uses 15 s), a failed body read is retried up to 3 times (backoff `min(2^n, 8)` seconds), and a failed download is retried up to 3 times (backoff `min(2^n, 30)` seconds).

---

## Project layout

```
mediafire-tool/v2/
├─ run.pyw                  Windowed entry point (no console; crashes go to crash.log)
├─ mfd/
│  ├─ __init__.py           Version, app name, module boundaries
│  ├─ mediafire.py          Talking to MediaFire: parsing, pagination, direct URLs, error codes
│  ├─ downloader.py         Talking to disk: queue, thread pool, resume, verification, atomic writes
│  ├─ failures.py           Remembering failures: 21 categories, failure list, exports
│  ├─ diagnostics.py        Remembering the environment: environment probe, OS × error cross-tab
│  ├─ storage.py            Remembering history: registry / JSON backends, dedupe, cap
│  ├─ paths.py              Data directory resolution (%LOCALAPPDATA%, override via MFD_DATA_DIR)
│  ├─ util.py               Filename sanitising, path length, size and duration formatting
│  └─ ui/                   Talking to the user: theme tokens, widgets, main window
│     ├─ theme.py           Dark / light palettes, DPI, font probing
│     ├─ widgets.py         Canvas-based controls (button, pill, progress bar, status dot, card)
│     └─ app.py             Main window: four tabs, event pump, close policy
├─ tests/
│  ├─ test_offline.py       Offline suite: local http.server fake direct links (Range / 403)
│  ├─ test_diagnostics.py   Diagnostics suite: category mapping, cross-tab, report shape
│  ├─ test_gui_smoke.py     UI smoke: window withdrawn, driven by update() only
│  └─ test_live.py          Live checks (requires MFD_LIVE=1, skipped by default)
├─ packaging/
│  ├─ build.spec            PyInstaller config (Tk data dirs, UCRT stripping, version info)
│  └─ build.bat             One-command build (finds Python 3.8 automatically)
├─ assets/                  Icon and version resource
├─ docs/
│  ├─ UI-CONTRACT.md        Contract between kernel and UI
│  └─ COMPATIBILITY.md      Compatibility matrix and error-handling table
├─ .github/ISSUE_TEMPLATE/  Issue template
├─ pyproject.toml           Packaging metadata (no third-party dependencies)
├─ CHANGELOG.md
├─ CONTRIBUTING.md
└─ LICENSE                  MIT
```

---

## Development and tests

```powershell
# Offline suite: no network, no registry, no user directories
python tests\test_offline.py

# UI smoke test: no visible window, no mainloop; needs a desktop session
python tests\test_gui_smoke.py

# Both
python -m unittest discover -s tests -v
```

`test_offline.py` spins up a fake MediaFire direct link with the standard library's `http.server` and covers filename sanitising, size/duration formatting, URL decoding, link parsing, error classification, path masking, failure-list persistence, the task model, and a resumable download including a simulated 403. The suite **needs no network**, so it runs the same locally and on CI.

Coding style, commit conventions and the PR checklist live in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Building the exe

```powershell
cd packaging
build.bat
```

`build.bat` looks for an interpreter in this order: `..\..\build-env\nuget38\tools\python.exe` → `py -3.8` → `python`; checks for PyInstaller; clears `..\build-temp`; then runs:

```
python -m PyInstaller --clean --noconfirm --distpath "..\dist" --workpath "..\build-temp" build.spec
```

Output: `..\dist\MediaFire批量下载器2.0.exe`, a single file.

Three things in `build.spec` are there for a reason, all learned the hard way:

1. **Build with Python 3.8** if you want any chance of running on Windows 7 (see PyInstaller issue #6621);
2. **Ship the Tk data directories**: `tcl8.6` / `tk8.6` are placed at `tcl/tcl8.6` and `tcl/tk8.6` inside the executable, otherwise the UI refuses to start on Win7;
3. **Strip the build machine's UCRT**: `ucrtbase.dll` and the `api-ms-win-core-*` forwarders get pulled in from the build machine, but their versions do not match Win7 — drop those and keep `api-ms-win-crt-*`, or the app fails at startup.

The full matrix and per-case remedies are in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

---

## Known limits

- **Public shares only**: private files require a login state, and this tool has none.
- **The API is not official**: it relies on MediaFire's share pages and public web API; a change on their side or aggressive rate limiting will affect the tool. Error codes and symptoms are tracked in `docs/COMPATIBILITY.md`.
- **Large folders take a few seconds to parse**: 100 entries per page means several round trips for a few hundred files (about 7 s for a typical folder).
- **Packaging is Windows-specific**: the source runs anywhere with Python and Tk, but `packaging/` and the Win7 caveats are Windows-only concerns.

---

## License

[MIT](LICENSE) © 2026 MediaFire Batch Downloader contributors

This project is not affiliated with, endorsed by, or sponsored by MediaFire. "MediaFire" is a trademark of its respective owner and is used here only to describe what the tool works with.

---

## Credits

- **MediaFire** for serving share pages and a public web API without requiring a login — without that, a tool like this could not exist.
- **[PyInstaller](https://pyinstaller.org/)** for single-file packaging, and for the Win7 pitfalls that had to be worked around (issue #6621 among them).
- **Python and Tkinter**: `http.client`, `ssl`, `hashlib` and `tkinter` from the standard library carry the entire stack — no third-party code is involved.
- **Users of the retired 5.0**: that version is gone, but the hard-won knowledge in its bug reports — error categories, Win7 patches, `more_chunks` pagination — carried straight into the 2.0 rewrite.
