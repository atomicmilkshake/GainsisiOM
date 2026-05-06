# GainsisiOM

**Gamma Indigo Sierra Search — by OM**

A Voidtools Everything-inspired desktop file search tool for Windows. Single standalone Python script, no external dependencies, no install required.

---

## Requirements

- Windows (Win32 Shell context menu requires Windows; rest is cross-platform Python)
- Python 3.11+ (pure stdlib — no pip installs)
- No ArcGIS, no third-party packages

---

## Quick Start

```
python GainsisiOM.py
```

On first launch, GainsisiOM indexes `D:\` and your user profile. Results appear live as you type.

---

## How It Works

GainsisiOM builds a SQLite database (`file_index_search.db`, stored in your user home directory) that records every file and folder under the indexed roots. The entire index is loaded into RAM at startup as compact `FileRecord` objects, making search nearly instantaneous regardless of index size.

- **Index is persistent.** Closing and reopening the app reloads the cached index immediately — no re-scan required.
- **Auto-refresh** runs every 5 minutes in the background (configurable via `AUTO_REFRESH_SECONDS`).
- **28 worker threads** (`NUM_THREADS`) crawl the file system in parallel during indexing.
- Search results are capped at **10,000 items** (`MAX_RESULTS`) for UI performance.

---

## GUI Overview

### Search Bar (Row 1)
The search bar spans the full window width. Typing triggers a live search after a short debounce (120 ms). Press **Enter** to force an immediate search. The **Down Arrow** key moves focus to the results list.

Search history (up to 50 entries) is accessible via the dropdown arrow or **Down Arrow** in the empty search box.

### Controls Row (Row 2)
| Control | Description |
|---|---|
| **Sort** dropdown | Sort results by Date, Name, Size, Path, Ext, or Type |
| **Desc** checkbox | Toggle sort direction (descending by default) |
| **Match Path** checkbox | When checked, plain search terms match against the full path in addition to the filename |
| **Syntax Help** | Opens the search syntax reference popup |
| **Browse & Index...** | Pick any folder to index (rebuild, asks for confirmation) |
| **Re-index Default** | Full rebuild of `D:\` and user profile (asks for confirmation) |
| **Refresh Now** | Incremental refresh — adds new files, removes deleted ones |
| **Clear Index** | Deletes all indexed data from the database (asks for confirmation) |
| **Index Info** | Shows record count, index version, build state, last indexed time, and indexed roots |
| **Export CSV** | Saves current search results to a UTF-8 CSV file |

### Results Treeview
Columns: **Name · Ext · Type · Size · Date · Path**

| Action | Result |
|---|---|
| **Double-click** (Name/Ext/Type/Size/Date columns) | Opens the file or folder |
| **Double-click** (Path column) | Opens the containing folder in Explorer |
| **Right-click** | Context menu: Open, Copy Path, Shell Menu… |
| **Enter** | Opens the focused item |
| **Ctrl+C** | Copies the full path of the focused item to the clipboard |

### Status Bar
Displays current status, current Match mode (Path or Name), time until next auto-refresh, and the last refresh timestamp.

---

## Search Syntax

GainsisiOM implements a superset of Voidtools Everything search syntax.

### Basic
| Syntax | Behavior |
|---|---|
| `word` | Name (or full path when Match Path is on) contains "word" |
| `"exact phrase"` | Exact phrase match |
| `word1 word2` | AND — both required |
| `word1 \| word2` | OR |
| `!word` | NOT |
| `(group)` or `<group>` | Grouping |

### Path & Location
| Syntax | Behavior |
|---|---|
| `path:folder` | Full path contains "folder" |
| `nopath:word` | Filename only (ignores path) |
| `parent:dir` | Immediate parent directory matches "dir" |
| `D:` | File is on drive D: |
| `\folder\` | Path contains that segment |

### File Types
| Syntax | Matches |
|---|---|
| `ext:pdf;docx` | Extension is pdf or docx (semicolon-separated) |
| `audio:` | mp3, wav, flac, aac, ogg, wma, m4a |
| `doc:` | pdf, doc, docx, xls, xlsx, ppt, pptx, txt, rtf |
| `exe:` | exe, msi, bat, cmd, ps1, com |
| `pic:` | jpg, png, gif, bmp, tif, webp, svg |
| `video:` | mp4, mkv, avi, mov, wmv, webm |
| `zip:` | zip, 7z, rar, tar, gz, bz2, xz |
| `file:` | Files only |
| `folder:` | Folders only |

### Size
| Syntax | Behavior |
|---|---|
| `size:>10mb` | Larger than 10 MB |
| `size:<500kb` | Smaller than 500 KB |
| `size:1mb..50mb` | Between 1 MB and 50 MB |

Size units: `b`, `kb`, `mb`, `gb`, `tb`. Named sizes: `empty` (0), `tiny` (<10 KB), `small` (<100 KB), `medium` (<1 MB), `large` (<16 MB), `huge` (≥16 MB).

### Date Modified
| Syntax | Behavior |
|---|---|
| `dm:today` | Modified today |
| `dm:yesterday` | Modified yesterday |
| `dm:thisweek` | Modified this calendar week |
| `dm:thismonth` | Modified this month |
| `dm:thisyear` | Modified this year |
| `dm:>2025-01-01` | Modified after a specific date |
| `dm:2025-01-01..2025-12-31` | Date range |

### Regex & Advanced
| Syntax | Behavior |
|---|---|
| `regex:pattern` | Python regex match on full path (case-insensitive) |
| `noregex:word` | Literal text, no wildcard or regex interpretation |
| `count:N` | Limit results to N items |

### Wildcard Support
`*` matches any sequence of characters; `?` matches any single character. Works in plain terms and in `path:`, `nopath:`, `parent:` values.

### Combining
```
ext:pdf path:reports dm:thismonth size:>1mb
ext:pdf;docx !draft path:projects
regex:^\w:\\.+\.log$ size:>10mb
```

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| **Enter** (search box) | Run search immediately |
| **Down Arrow** (search box) | Move focus to first result |
| **Enter** (results list) | Open focused item |
| **Ctrl+C** (results list) | Copy full path |
| **Ctrl+U** | Toggle Match Path |
| **F1** | Open Syntax Help |
| **Escape** | Cancel in-progress indexing |

---

## Settings

Settings are saved automatically on close to `%USERPROFILE%\gainsisiom_settings.json`. Stored values:
- Last 50 search history entries
- Match Path on/off
- Window size and position

Window size is restored on next launch, clamped to a minimum of **900×480** pixels. Default size on first launch: **1400×750**.

---

## Files

| File | Description |
|---|---|
| `GainsisiOM.py` | Single-file application — everything lives here |
| `%USERPROFILE%\file_index_search.db` | SQLite index database (generated; safe to delete — forces full re-index) |
| `%USERPROFILE%\gainsisiom_settings.json` | User settings (search history, window geometry, Match Path state) |

---

## Configuration Constants

These are at the top of `GainsisiOM.py` and can be adjusted:

| Constant | Default | Description |
|---|---|---|
| `NUM_THREADS` | 28 | Worker threads for file system crawl |
| `MAX_RESULTS` | 10,000 | Maximum rows shown in results |
| `BATCH_SIZE` | 500 | SQLite insert batch size |
| `SEARCH_DEBOUNCE` | 120 ms | Delay before search fires after keystroke |
| `AUTO_REFRESH_SECONDS` | 300 | Background refresh interval (seconds) |
| `MAX_SEARCH_HISTORY` | 50 | Number of search history entries kept |

---

## Architecture Notes

- **Single-file, pure stdlib.** No splitting into modules unless explicitly requested.
- **RAM-resident index.** All `FileRecord` objects (compact `__slots__` dataclasses) are loaded from SQLite into a Python list at startup. Search never touches disk.
- **Pre-lowered fields.** `name_lower`, `fullpath_lower`, `ext_lower` are computed once at load time, eliminating per-search `.lower()` calls.
- **Fast path.** Single-word queries with no operators, wildcards, or filters bypass the full AST evaluator and run as a raw `in` membership test — covers ~80% of typical searches.
- **Non-blocking search.** Filtering runs in a `ThreadPoolExecutor(max_workers=1)` thread; results are posted back to the main thread via a queue polled every 50 ms. The UI never blocks.
- **Windows Shell context menu.** Implemented via ctypes COM calls to `IContextMenu`/`IContextMenu2`/`IContextMenu3` — no pywin32 required.
- **SQLite WAL mode.** Indexing and reading run concurrently without locking conflicts.
