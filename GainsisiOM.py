"""
GainsisiOM  -  Gamma Indigo Sierra Search (GIS Search) by OM.
Voidtools Everything-inspired desktop search.
Standalone Python script for Windows using tkinter and sqlite3.
"""

import csv
import ctypes
import ctypes.wintypes
import datetime
import os
import queue
import re
import json
import sqlite3
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
APP_NAME = "GainsisiOM"
DB_NAME = "file_index_search.db"
INDEX_VERSION = "2"

NUM_THREADS = 28
MAX_RESULTS = 10_000
BATCH_SIZE = 500
STATUS_INTERVAL = 0.5
SEARCH_DEBOUNCE = 120
AUTO_REFRESH_SECONDS = 300
SETTINGS_FILE = "gainsisiom_settings.json"
MAX_SEARCH_HISTORY = 50

STATUS_IDLE = "Ready"
SEARCH_PLACEHOLDER = (
    "Search filenames... (Everything syntax: | ! \"\" path: ext: parent: size: dm: regex:)"
)


# ---------------------------------------------------------------------------
# FILE RECORD  (compact __slots__ dataclass — ~4x smaller than plain dict)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FileRecord:
    fullpath: str
    name: str
    ext: str
    size: object        # int | None
    mtime: object       # float | None
    is_dir: bool
    depth: int
    # Pre-lowered search fields — paid once at load, zero cost per search
    name_lower: str
    fullpath_lower: str
    ext_lower: str      # no leading dot, already lowercase


# ---------------------------------------------------------------------------
# WINDOWS SHELL CONTEXT MENU  (IContextMenu via ctypes, no pywin32)
# ---------------------------------------------------------------------------
class ShellContextMenu:
    """Show the genuine Windows Explorer shell context menu for a path."""

    # --- COM GUIDs ---
    _IID_IShellFolder   = "{000214E6-0000-0000-C000-000000000046}"
    _IID_IContextMenu   = "{000214E4-0000-0000-C000-000000000046}"
    _IID_IContextMenu2  = "{000214F4-0000-0000-C000-000000000046}"
    _IID_IContextMenu3  = "{BCFCE0A0-EC17-11D0-8D10-00A0C90F2719}"

    _CMF_EXPLORE        = 0x00000004
    _CMF_CANRENAME      = 0x00000010
    _TPM_RETURNCMD      = 0x0100
    _TPM_RIGHTBUTTON    = 0x0002
    _CMIC_MASK_UNICODE  = 0x00004000
    _SW_SHOWNORMAL      = 1

    _WM_INITMENUPOPUP   = 0x0117
    _WM_MEASUREITEM     = 0x002C
    _WM_DRAWITEM        = 0x002B
    _GWLP_WNDPROC       = -4

    _shell32  = ctypes.windll.shell32
    _ole32    = ctypes.windll.ole32
    _user32   = ctypes.windll.user32

    @staticmethod
    def _guid_bytes(guid_str: str) -> bytes:
        import uuid
        return uuid.UUID(guid_str).bytes_le

    @staticmethod
    def _make_guid(guid_str: str):
        data = ShellContextMenu._guid_bytes(guid_str)
        buf = (ctypes.c_byte * 16)(*data)
        return buf

    def show(self, hwnd: int, fullpath: str, x: int, y: int):
        try:
            self._show_impl(hwnd, fullpath, x, y)
        except Exception:
            pass

    def _show_impl(self, hwnd: int, fullpath: str, x: int, y: int):
        ole32   = self._ole32
        shell32 = self._shell32
        user32  = self._user32

        ole32.CoInitialize(None)

        # --- SHParseDisplayName -> absolute PIDL ---
        pidl        = ctypes.c_void_p()
        sfgao       = ctypes.c_ulong(0)
        hr = shell32.SHParseDisplayName(
            ctypes.c_wchar_p(fullpath), None,
            ctypes.byref(pidl), 0, ctypes.byref(sfgao)
        )
        if hr != 0 or not pidl:
            return

        # --- SHBindToParent -> IShellFolder + child PIDL ---
        psf          = ctypes.c_void_p()
        pidl_child   = ctypes.c_void_p()
        iid_sf       = self._make_guid(self._IID_IShellFolder)
        hr = shell32.SHBindToParent(
            pidl, iid_sf,
            ctypes.byref(psf), ctypes.byref(pidl_child)
        )
        if hr != 0 or not psf:
            ole32.CoTaskMemFree(pidl)
            return

        # --- IShellFolder::GetUIObjectOf -> IContextMenu ---
        pcm         = ctypes.c_void_p()
        iid_cm      = self._make_guid(self._IID_IContextMenu)
        # psf is an IShellFolder COM ptr; call GetUIObjectOf through vtable
        # vtable offsets (IUnknown=0,1,2; IShellFolder: ParseDisplayName=3,
        #   EnumObjects=4, BindToObject=5, BindToStorage=6,
        #   CompareIDs=7, CreateViewObject=8, GetAttributesOf=9,
        #   GetUIObjectOf=10)
        psf_ptr = ctypes.cast(psf, ctypes.POINTER(ctypes.c_void_p))
        vtable  = ctypes.cast(psf_ptr[0], ctypes.POINTER(ctypes.c_void_p))
        GETUI_IDX = 10
        GetUIObjectOf = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,   # this
            ctypes.wintypes.HWND,
            ctypes.c_uint,     # cidl
            ctypes.POINTER(ctypes.c_void_p),  # apidl
            ctypes.POINTER(ctypes.c_byte * 16),  # riid
            ctypes.POINTER(ctypes.c_ulong),   # rgfReserved
            ctypes.POINTER(ctypes.c_void_p),  # ppv
        )(vtable[GETUI_IDX])

        child_arr = (ctypes.c_void_p * 1)(pidl_child)
        hr = GetUIObjectOf(
            psf, hwnd, 1,
            ctypes.cast(child_arr, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(iid_cm, ctypes.POINTER(ctypes.c_byte * 16)),
            None,
            ctypes.byref(pcm)
        )
        if hr != 0 or not pcm:
            self._release(psf)
            ole32.CoTaskMemFree(pidl)
            return

        # --- QueryInterface for IContextMenu2 and IContextMenu3 ---
        pcm2 = ctypes.c_void_p()
        pcm3 = ctypes.c_void_p()
        iid_cm2 = self._make_guid(self._IID_IContextMenu2)
        iid_cm3 = self._make_guid(self._IID_IContextMenu3)
        self._qi(pcm, iid_cm2, pcm2)
        self._qi(pcm, iid_cm3, pcm3)

        # --- CreatePopupMenu ---
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            self._release(pcm3); self._release(pcm2); self._release(pcm)
            self._release(psf); ole32.CoTaskMemFree(pidl)
            return

        # --- QueryContextMenu ---
        pcm_vtbl = self._vtable(pcm)
        QueryContextMenu = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
        )(pcm_vtbl[3])
        QueryContextMenu(pcm, hmenu, 0, 1, 0x7FFF,
                         self._CMF_EXPLORE | self._CMF_CANRENAME)

        # --- Fix pointer-sized API signatures for 64-bit Windows ---
        user32.GetWindowLongPtrW.restype  = ctypes.c_longlong
        user32.GetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype  = ctypes.c_longlong
        user32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        user32.CallWindowProcW.restype    = ctypes.c_longlong
        user32.CallWindowProcW.argtypes   = [
            ctypes.c_longlong, ctypes.wintypes.HWND,
            ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        ]

        # --- Subclass HWND to handle owner-draw shell menu messages ---
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong,
            ctypes.wintypes.HWND, ctypes.c_uint,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        )

        best_pcm   = pcm3 if pcm3 else (pcm2 if pcm2 else None)
        orig_proc_val = user32.GetWindowLongPtrW(hwnd, self._GWLP_WNDPROC)

        def _wndproc(hwnd_, msg, wp, lp):
            if best_pcm and msg in (self._WM_INITMENUPOPUP,
                                    self._WM_MEASUREITEM,
                                    self._WM_DRAWITEM):
                if pcm3:
                    hm2 = ctypes.WINFUNCTYPE(
                        ctypes.c_long,   # use c_long, not HRESULT — avoids OSError on E_FAIL
                        ctypes.c_void_p, ctypes.wintypes.HWND,
                        ctypes.c_uint, ctypes.wintypes.WPARAM,
                        ctypes.wintypes.LPARAM, ctypes.POINTER(ctypes.c_long)
                    )(self._vtable(pcm3)[6])
                    result = ctypes.c_long(0)
                    hm2(pcm3, hwnd_, msg, wp, lp, ctypes.byref(result))
                elif pcm2:
                    hm = ctypes.WINFUNCTYPE(
                        ctypes.c_long,   # use c_long, not HRESULT — avoids OSError on E_FAIL
                        ctypes.c_void_p, ctypes.wintypes.HWND,
                        ctypes.c_uint, ctypes.wintypes.WPARAM,
                        ctypes.wintypes.LPARAM
                    )(self._vtable(pcm2)[3])
                    hm(pcm2, hwnd_, msg, wp, lp)
            return user32.CallWindowProcW(
                orig_proc_val, hwnd_, msg, wp, lp
            )

        # Store on self to prevent GC while the menu is open (classic ctypes callback pitfall)
        self._wndproc_cb = WNDPROC(_wndproc)
        cb_addr = ctypes.cast(self._wndproc_cb, ctypes.c_void_p).value
        user32.SetWindowLongPtrW(hwnd, self._GWLP_WNDPROC, cb_addr)

        # --- Show menu ---
        cmd_id = user32.TrackPopupMenuEx(
            hmenu,
            self._TPM_RETURNCMD | self._TPM_RIGHTBUTTON,
            x, y, hwnd, None
        )

        # --- Restore wndproc immediately after menu returns ---
        user32.SetWindowLongPtrW(hwnd, self._GWLP_WNDPROC, orig_proc_val)
        self._wndproc_cb = None  # Safe to release — original proc already restored

        # --- Invoke command if selected ---
        if cmd_id > 0:
            class CMINVOKECOMMANDINFOEX(ctypes.Structure):
                _fields_ = [
                    ("cbSize",         ctypes.c_uint),
                    ("fMask",          ctypes.c_ulong),
                    ("hwnd",           ctypes.wintypes.HWND),
                    ("lpVerb",         ctypes.c_char_p),
                    ("lpParameters",   ctypes.c_char_p),
                    ("lpDirectory",    ctypes.c_char_p),
                    ("nShow",          ctypes.c_int),
                    ("dwHotKey",       ctypes.c_uint),
                    ("hIcon",          ctypes.c_void_p),
                    ("lpTitle",        ctypes.c_char_p),
                    ("lpVerbW",        ctypes.c_wchar_p),
                    ("lpParametersW",  ctypes.c_wchar_p),
                    ("lpDirectoryW",   ctypes.c_wchar_p),
                    ("lpTitleW",       ctypes.c_wchar_p),
                    ("ptInvoke",       ctypes.wintypes.POINT),
                ]
            ici = CMINVOKECOMMANDINFOEX()
            ici.cbSize       = ctypes.sizeof(CMINVOKECOMMANDINFOEX)
            ici.fMask        = self._CMIC_MASK_UNICODE
            ici.hwnd         = hwnd
            ici.lpVerb       = ctypes.cast(
                ctypes.c_void_p(cmd_id - 1), ctypes.c_char_p
            )
            ici.lpVerbW      = ctypes.cast(
                ctypes.c_void_p(cmd_id - 1), ctypes.c_wchar_p
            )
            ici.nShow        = self._SW_SHOWNORMAL

            InvokeCommand = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(CMINVOKECOMMANDINFOEX)
            )(self._vtable(pcm)[4])
            InvokeCommand(pcm, ctypes.byref(ici))

        # --- Cleanup ---
        user32.DestroyMenu(hmenu)
        self._release(pcm3)
        self._release(pcm2)
        self._release(pcm)
        self._release(psf)
        ole32.CoTaskMemFree(pidl)

    @staticmethod
    def _vtable(com_ptr):
        ptr = ctypes.cast(com_ptr, ctypes.POINTER(ctypes.c_void_p))
        return ctypes.cast(ptr[0], ctypes.POINTER(ctypes.c_void_p))

    @staticmethod
    def _release(com_ptr):
        if com_ptr:
            release_fn = ctypes.WINFUNCTYPE(
                ctypes.c_ulong, ctypes.c_void_p
            )(ShellContextMenu._vtable(com_ptr)[2])
            release_fn(com_ptr)

    @staticmethod
    def _qi(com_ptr, iid, out_ptr):
        if not com_ptr:
            return
        qi_fn = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_byte * 16),
            ctypes.POINTER(ctypes.c_void_p)
        )(ShellContextMenu._vtable(com_ptr)[0])
        qi_fn(com_ptr,
              ctypes.cast(iid, ctypes.POINTER(ctypes.c_byte * 16)),
              ctypes.byref(out_ptr))


# ---------------------------------------------------------------------------
# EVERYTHING-LIKE QUERY ENGINE
# ---------------------------------------------------------------------------
class QuerySyntaxError(Exception):
    pass


class EverythingQueryEngine:
    """
    Evaluates Everything-style queries against in-memory rows.

    Supported:
      - implicit AND via spaces
      - OR via |
      - NOT via !
      - grouping via () and <>
      - quoted phrases
      - wildcard terms (* ? [])
      - path:, nopath:, ext:, size:, dm:/datemodified:, file:, folder:
      - regex:, noregex:, count:
      - common macros: audio:, zip:, doc:, exe:, pic:, video:
    """

    _MACRO_EXTS = {
        "audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a"},
        "zip": {"zip", "7z", "rar", "tar", "gz", "bz2", "xz"},
        "doc": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf"},
        "exe": {"exe", "msi", "bat", "cmd", "ps1", "com"},
        "pic": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "svg"},
        "video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v"},
    }

    def __init__(self):
        self._tokens = []
        self._idx = 0

    def build(self, query: str):
        text = (query or "").strip()
        if not text:
            return {"ast": None, "limit": MAX_RESULTS}

        tokens = self._tokenize(text)
        count_limit, cleaned_tokens = self._extract_count(tokens)
        self._tokens = cleaned_tokens
        self._idx = 0

        if not self._tokens:
            return {"ast": None, "limit": count_limit or MAX_RESULTS}

        ast = self._parse_or()
        if self._peek() is not None:
            raise QuerySyntaxError(f"Unexpected token: {self._peek()}")

        ast = self._precompile_regex(ast)
        return {"ast": ast, "limit": count_limit or MAX_RESULTS}

    def evaluate(self, plan, row, match_path: bool = True) -> bool:
        ast = plan.get("ast")
        if ast is None:
            return True
        return self._eval_node(ast, row, match_path)

    def _precompile_regex(self, ast):
        """Walk AST and replace regex: term nodes with pre-compiled patterns."""
        if ast is None:
            return None
        kind = ast[0]
        if kind == "term":
            tok = ast[1]
            if tok.lower().startswith("regex:"):
                pattern = tok.split(":", 1)[1]
                try:
                    return ("regex_compiled", re.compile(pattern, re.IGNORECASE))
                except re.error:
                    return ("regex_error",)
            return ast
        if kind in ("and", "or"):
            return (kind, self._precompile_regex(ast[1]), self._precompile_regex(ast[2]))
        if kind == "not":
            return ("not", self._precompile_regex(ast[1]))
        return ast

    def _tokenize(self, text: str):
        tokens = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch.isspace():
                i += 1
                continue
            if ch in ("|", "!", "(", ")", "<", ">"):
                tokens.append(ch)
                i += 1
                continue
            if ch == '"':
                i += 1
                buf = []
                while i < n:
                    if text[i] == '"':
                        break
                    if text[i] == "\\" and i + 1 < n:
                        i += 1
                        buf.append(text[i])
                    else:
                        buf.append(text[i])
                    i += 1
                if i >= n or text[i] != '"':
                    raise QuerySyntaxError("Unclosed quote")
                i += 1
                tokens.append('"' + "".join(buf) + '"')
                continue

            start = i
            while i < n and (not text[i].isspace()) and text[i] not in ("|", "!", "(", ")", "<", ">"):
                i += 1
            tokens.append(text[start:i])
        return tokens

    def _extract_count(self, tokens):
        limit = None
        out = []
        for tok in tokens:
            low = tok.lower()
            if low.startswith("count:"):
                value = tok.split(":", 1)[1].strip()
                if value.isdigit():
                    limit = max(1, int(value))
                continue
            out.append(tok)
        return limit, out

    def _peek(self):
        if self._idx >= len(self._tokens):
            return None
        return self._tokens[self._idx]

    def _consume(self):
        tok = self._peek()
        if tok is None:
            return None
        self._idx += 1
        return tok

    def _starts_primary(self, tok):
        return tok is not None and tok not in ("|", ")", ">")

    def _parse_or(self):
        node = self._parse_and()
        while self._peek() == "|":
            self._consume()
            rhs = self._parse_and()
            node = ("or", node, rhs)
        return node

    def _parse_and(self):
        node = self._parse_not()
        while self._starts_primary(self._peek()):
            rhs = self._parse_not()
            node = ("and", node, rhs)
        return node

    def _parse_not(self):
        if self._peek() == "!":
            self._consume()
            return ("not", self._parse_not())
        return self._parse_primary()

    def _parse_primary(self):
        tok = self._peek()
        if tok is None:
            raise QuerySyntaxError("Unexpected end of query")

        if tok in ("(", "<"):
            opener = self._consume()
            closer = ")" if opener == "(" else ">"
            node = self._parse_or()
            if self._peek() != closer:
                raise QuerySyntaxError(f"Missing closing {closer}")
            self._consume()
            return node

        return ("term", self._consume())

    def _eval_node(self, node, row, match_path: bool = True):
        kind = node[0]
        if kind == "term":
            return self._eval_term(node[1], row, match_path)
        if kind == "and":
            return self._eval_node(node[1], row, match_path) and self._eval_node(node[2], row, match_path)
        if kind == "or":
            return self._eval_node(node[1], row, match_path) or self._eval_node(node[2], row, match_path)
        if kind == "not":
            return not self._eval_node(node[1], row, match_path)
        if kind == "regex_compiled":
            return node[1].search(row.fullpath) is not None
        if kind == "regex_error":
            return False
        return False

    @staticmethod
    def _normalize_date_value(text: str):
        value = text.strip().lower()
        now = datetime.datetime.now()

        constants = {
            "today": datetime.datetime(now.year, now.month, now.day),
            "yesterday": datetime.datetime(now.year, now.month, now.day) - datetime.timedelta(days=1),
            "thisweek": datetime.datetime(now.year, now.month, now.day) - datetime.timedelta(days=now.weekday()),
            "thismonth": datetime.datetime(now.year, now.month, 1),
            "thisyear": datetime.datetime(now.year, 1, 1),
        }
        if value in constants:
            return constants[value]

        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m", "%Y"):
            try:
                dt = datetime.datetime.strptime(value, fmt)
                return dt
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_size_value(text: str):
        s = text.strip().lower()
        constants = {
            "empty": 0,
            "tiny": 10 * 1024,
            "small": 100 * 1024,
            "medium": 1024 * 1024,
            "large": 16 * 1024 * 1024,
            "huge": 128 * 1024 * 1024,
        }
        if s in constants:
            return constants[s]

        m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(b|kb|mb|gb|tb)?", s)
        if not m:
            return None
        num = float(m.group(1))
        unit = (m.group(2) or "b").lower()
        mult = {
            "b": 1,
            "kb": 1024,
            "mb": 1024 ** 2,
            "gb": 1024 ** 3,
            "tb": 1024 ** 4,
        }.get(unit, 1)
        return int(num * mult)

    def _match_text(self, text: str, target: str):
        src = (target or "").lower()
        q = text.lower()
        if any(ch in q for ch in ("*", "?", "[")):
            if q.startswith("*") or q.endswith("*"):
                return fnmatch(src, q)
            return fnmatch(src, f"*{q}*")
        return q in src

    @staticmethod
    def _match_lower(q_lower: str, target_lower: str) -> bool:
        """Match a pre-lowered query against a pre-lowered target — zero extra .lower() calls."""
        if any(ch in q_lower for ch in ("*", "?", "[")):
            if q_lower.startswith("*") or q_lower.endswith("*"):
                return fnmatch(target_lower, q_lower)
            return fnmatch(target_lower, f"*{q_lower}*")
        return q_lower in target_lower

    def _match_compare(self, val, text, parse_value):
        raw = text.strip().lower()
        if ".." in raw:
            left, right = raw.split("..", 1)
            lo = parse_value(left)
            hi = parse_value(right)
            if lo is None or hi is None:
                return False
            return lo <= val <= hi

        for op in (">=", "<=", ">", "<", "="):
            if raw.startswith(op):
                rhs = parse_value(raw[len(op):])
                if rhs is None:
                    return False
                if op == ">=":
                    return val >= rhs
                if op == "<=":
                    return val <= rhs
                if op == ">":
                    return val > rhs
                if op == "<":
                    return val < rhs
                return val == rhs

        rhs = parse_value(raw)
        if rhs is None:
            return False
        return val == rhs

    def _eval_term(self, token: str, row, match_path: bool = True):
        name_l   = row.name_lower
        path_l   = row.fullpath_lower
        ext_l    = row.ext_lower
        is_dir   = row.is_dir
        size_val = row.size
        mtime    = row.mtime
        fullpath = row.fullpath  # original case — needed for regex

        lower_token = token.lower()

        if token.startswith('"') and token.endswith('"'):
            q = lower_token[1:-1]
            if match_path:
                return self._match_lower(q, name_l) or self._match_lower(q, path_l)
            return self._match_lower(q, name_l)

        # Shortcut macros with no explicit value.
        if lower_token.endswith(":") and lower_token[:-1] in self._MACRO_EXTS:
            return ext_l in self._MACRO_EXTS[lower_token[:-1]]

        if ":" in token:
            key, raw_val = token.split(":", 1)
            key = key.lower().strip()
            val = raw_val.strip()

            if key in ("file", "files") and val == "":
                return not is_dir
            if key in ("folder", "folders") and val == "":
                return is_dir

            if key == "path":
                return self._match_lower((val if val else "*").lower(), path_l)
            if key == "nopath":
                return self._match_lower((val if val else "*").lower(), name_l)

            if key == "ext":
                options = [x.strip().lstrip(".").lower() for x in val.split(";") if x.strip()]
                return ext_l in options if options else False

            if key == "parent":
                # Match items whose immediate parent directory path contains val
                parent_l = path_l.rsplit("\\", 1)[0] if "\\" in path_l else path_l
                return self._match_lower(val.lower().replace("/", "\\"), parent_l)

            if key == "regex":
                try:
                    return re.search(val, fullpath, flags=re.IGNORECASE) is not None
                except re.error:
                    return False

            if key == "noregex":
                v = val.lower()
                return self._match_lower(v, name_l) or self._match_lower(v, path_l)

            if key == "size":
                return self._match_compare(size_val or 0, val, self._parse_size_value)

            if key in ("dm", "datemodified"):
                if not mtime:
                    return False
                dt = datetime.datetime.fromtimestamp(mtime)
                return self._match_compare(dt, val, self._normalize_date_value)

            if key in self._MACRO_EXTS and val == "":
                return ext_l in self._MACRO_EXTS[key]

            # Unknown function names are treated as plain text to avoid hard failures.
            merged_l = (f"{key}:{val}" if val else f"{key}:").lower()
            return self._match_lower(merged_l, name_l) or self._match_lower(merged_l, path_l)

        # Drive and partial path search behavior.
        if re.fullmatch(r"[a-zA-Z]:", token):
            return path_l.startswith(lower_token + "\\")

        if "\\" in token or "/" in token:
            return self._match_lower(lower_token.replace("/", "\\"), path_l)

        if match_path:
            return self._match_lower(lower_token, name_l) or self._match_lower(lower_token, path_l)
        return self._match_lower(lower_token, name_l)


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
class FileIndexDB:
    """SQLite-backed file index and metadata."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA cache_size=-65536")  # ~64 MB cache
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    def _ensure_schema(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    fullpath TEXT    UNIQUE NOT NULL,
                    name     TEXT    NOT NULL,
                    ext      TEXT,
                    size     INTEGER,
                    mtime    REAL,
                    is_dir   INTEGER,
                    depth    INTEGER
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_name ON files(name COLLATE NOCASE)"
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ext ON files(ext)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_fullpath ON files(fullpath)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime)")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    def clear(self):
        with self.conn:
            self.conn.execute("DELETE FROM files")
        self.conn.execute("VACUUM")

    def upsert_files(self, rows: list):
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO files "
                "(fullpath, name, ext, size, mtime, is_dir, depth) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    @staticmethod
    def _to_like_prefix(path_prefix: str):
        p = path_prefix.replace("/", "\\")
        escaped = p.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        if not escaped.endswith("\\"):
            escaped += "\\"
        return escaped + "%"

    def delete_missing_under_roots(self, roots: list, seen_paths: set):
        with self.conn:
            self.conn.execute("DROP TABLE IF EXISTS _seen_paths")
            self.conn.execute("CREATE TEMP TABLE _seen_paths(fullpath TEXT PRIMARY KEY)")
            if seen_paths:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO _seen_paths(fullpath) VALUES (?)",
                    [(p,) for p in seen_paths],
                )
            for root in roots:
                self.conn.execute(
                    "DELETE FROM files "
                    "WHERE fullpath LIKE ? ESCAPE '!' "
                    "AND fullpath NOT IN (SELECT fullpath FROM _seen_paths)",
                    (self._to_like_prefix(root),),
                )
            self.conn.execute("DROP TABLE IF EXISTS _seen_paths")

    def get_file_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()
        return row[0] if row else 0

    def get_all_rows(self) -> list:
        cursor = self.conn.execute(
            "SELECT fullpath, name, ext, size, mtime, is_dir, depth FROM files"
        )
        return [
            FileRecord(
                fullpath=row[0],
                name=row[1],
                ext=row[2] or "",
                size=row[3],
                mtime=row[4],
                is_dir=bool(row[5]),
                depth=row[6],
                name_lower=(row[1] or "").lower(),
                fullpath_lower=(row[0] or "").lower(),
                ext_lower=(row[2] or "").lstrip(".").lower(),
            )
            for row in cursor
        ]

    def set_meta(self, key: str, value: str):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def update_index_metadata(self, roots: list, build_state: str):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        normalized_roots = "|".join(sorted({str(Path(r)) for r in roots}))
        self.set_meta("index_version", INDEX_VERSION)
        self.set_meta("indexed_roots", normalized_roots)
        self.set_meta("last_indexed", now)
        self.set_meta("build_state", build_state)

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# INDEXER
# ---------------------------------------------------------------------------
class FileIndexer:
    """Multithreaded BFS file indexer with rebuild/refresh modes."""

    def __init__(self, db: FileIndexDB, root_paths: list, mode: str = "refresh", max_depth: int = 40):
        self.db = db
        self.root_paths = [Path(r) for r in root_paths if Path(r).exists()]
        self.mode = mode  # rebuild | refresh
        self.max_depth = max_depth
        self.cancelled = False

        self.status_queue = queue.Queue()
        self.work_queue = queue.Queue()
        self.result_queue = queue.Queue(maxsize=25_000)

        self._seen_lock = threading.Lock()
        self._seen_paths = set()

    def _remember_path(self, path_text: str):
        with self._seen_lock:
            self._seen_paths.add(path_text)

    def _worker(self):
        while True:
            item = self.work_queue.get()
            if item is None:
                self.work_queue.task_done()
                return

            path, depth = item
            try:
                if self.cancelled or depth > self.max_depth:
                    continue

                try:
                    for child in path.iterdir():
                        if self.cancelled:
                            break
                        try:
                            st = child.stat()
                            child_path = str(child)
                            self._remember_path(child_path)
                            self.result_queue.put(
                                (
                                    child_path,
                                    child.name,
                                    child.suffix.lower(),
                                    st.st_size,
                                    st.st_mtime,
                                    int(child.is_dir()),
                                    depth,
                                )
                            )
                        except (PermissionError, OSError):
                            continue

                        if child.is_dir() and (not child.is_symlink()) and depth < self.max_depth:
                            self.work_queue.put((child, depth + 1))
                except (PermissionError, OSError):
                    pass
            finally:
                self.work_queue.task_done()

    def _writer(self):
        batch = []
        total_written = 0
        last_status = 0.0

        while True:
            try:
                row = self.result_queue.get(timeout=0.4)
            except queue.Empty:
                if batch:
                    self.db.upsert_files(batch)
                    total_written += len(batch)
                    batch.clear()
                continue

            if row is None:
                break

            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                self.db.upsert_files(batch)
                total_written += len(batch)
                batch.clear()

                now = time.time()
                if now - last_status >= STATUS_INTERVAL:
                    self.status_queue.put(("progress", total_written))
                    last_status = now

        if batch:
            self.db.upsert_files(batch)
            total_written += len(batch)
            self.status_queue.put(("progress", total_written))

    def run(self) -> tuple:
        start = time.time()
        self.cancelled = False

        if not self.root_paths:
            return False, "No valid root paths to index."

        if self.mode == "rebuild":
            try:
                self.db.clear()
            except Exception as exc:
                return False, f"Failed to clear index: {exc}"

        for root in self.root_paths:
            self.work_queue.put((root, 0))

        self.status_queue.put(("phase", f"Scanning {len(self.root_paths)} root(s)..."))

        writer = threading.Thread(target=self._writer, daemon=True, name="idx-writer")
        writer.start()

        workers = [
            threading.Thread(target=self._worker, daemon=True, name=f"idx-worker-{i}")
            for i in range(NUM_THREADS)
        ]
        for w in workers:
            w.start()

        self.work_queue.join()

        for _ in workers:
            self.work_queue.put(None)
        for w in workers:
            w.join()

        self.status_queue.put(("phase", "Writing index changes..."))
        self.result_queue.put(None)
        writer.join()

        if self.cancelled:
            return False, "Indexing cancelled."

        if self.mode == "refresh":
            self.status_queue.put(("phase", "Finalizing refresh..."))
            self.db.delete_missing_under_roots([str(p) for p in self.root_paths], self._seen_paths)

        count = self.db.get_file_count()
        elapsed = time.time() - start
        msg = (
            f"Index update complete: {count:,} records in {elapsed:.1f}s "
            f"({self.mode}, {NUM_THREADS} threads)"
        )
        self.status_queue.put(("done", msg))
        return True, msg

    def cancel(self):
        self.cancelled = True


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class EverythingGUIApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1400x750")
        self.root.minsize(900, 480)

        self.db = None
        self.indexer = None
        self.is_indexing = False

        self._query_engine = EverythingQueryEngine()
        self._ram_rows = []
        self._row_data = {}
        self._sort_state = {}

        self._search_executor = ThreadPoolExecutor(max_workers=1)
        self._search_gen = 0
        self._search_pending_results = {}
        self._search_result_queue = queue.SimpleQueue()

        self._status_base = STATUS_IDLE
        self._next_refresh_epoch = None
        self._last_refresh_text = "Never"

        self.default_roots = self._build_default_roots()

        self.settings = self._load_settings()
        if "window_geometry" in self.settings:
            try:
                saved = self.settings["window_geometry"]
                # Parse WxH+X+Y and clamp to minimum size
                import re as _re
                _m = _re.match(r"(\d+)x(\d+)(.*)", saved)
                if _m:
                    _w = max(900, int(_m.group(1)))
                    _h = max(480, int(_m.group(2)))
                    saved = f"{_w}x{_h}{_m.group(3)}"
                self.root.geometry(saved)
            except Exception:
                pass
        self.search_history = self.settings.get("search_history", [])
        self.match_path_var = tk.BooleanVar(value=self.settings.get("match_path", True))

        self._setup_ui()
        self._load_db()
        self._load_ram_cache()

        self._poll_status()
        self._start_scheduler()
        self._poll_search_results()

        self._update_history_combobox()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Startup auto-index disabled — use buttons to index manually.

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _build_default_roots(self):
        roots = [r"D:\\", str(Path.home())]
        out = []
        for p in roots:
            if Path(p).exists():
                out.append(str(Path(p)))
        return out

    def _load_settings(self):
        """Load persistent settings including search history and preferences. Highest impact: survives app restarts."""
        try:
            path = os.path.join(os.path.expanduser("~"), SETTINGS_FILE)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings(self):
        """Save settings atomically for reliability."""
        try:
            path = os.path.join(os.path.expanduser("~"), SETTINGS_FILE)
            data = {
                "search_history": self.search_history[-MAX_SEARCH_HISTORY:],
                "match_path": self.match_path_var.get(),
                "window_geometry": self.root.geometry(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _add_to_search_history(self, query: str):
        """Add query to history if meaningful. Dedup and keep recent first for highest convenience."""
        if not query or query == SEARCH_PLACEHOLDER or len(query) < 2:
            return
        if query in self.search_history:
            self.search_history.remove(query)
        self.search_history.insert(0, query)
        self.search_history = self.search_history[:MAX_SEARCH_HISTORY]
        self._update_history_combobox()

    def _update_history_combobox(self):
        """Update Combobox values for dropdown."""
        if hasattr(self, 'search_entry') and isinstance(self.search_entry, ttk.Combobox):
            self.search_entry['values'] = self.search_history

    def _show_search_history(self, event=None):
        """Show history dropdown on demand (like Everything Ctrl+Space / down arrow)."""
        if hasattr(self, 'search_entry') and isinstance(self.search_entry, ttk.Combobox):
            self.search_entry.event_generate('<Button-1>')  # Simulate click to show dropdown

    def _on_match_path_changed(self):
        """Toggle Match Path and re-run current search for immediate feedback (highest impact UX)."""
        self._save_settings()
        if self.search_var.get() and self.search_var.get() != SEARCH_PLACEHOLDER:
            self._run_search()

    def _toggle_match_path(self, event=None):
        """Keyboard toggle (Ctrl+U like Everything) for highest productivity."""
        self.match_path_var.set(not self.match_path_var.get())
        self._on_match_path_changed()

    def _show_syntax_help(self):
        """Show comprehensive syntax glossary popup (Everything Help > Search syntax parity, highest convenience)."""
        help_win = tk.Toplevel(self.root)
        help_win.title("GainsisiOM Search Syntax Help")
        help_win.geometry("700x500")
        help_win.transient(self.root)

        text = tk.Text(help_win, wrap=tk.WORD, font=("Consolas", 10), padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)

        help_content = """
GAINSISIOM SEARCH SYNTAX (Everything-compatible + enhancements)

BASIC:
  word          - matches name or path containing "word"
  "exact phrase" - matches exact phrase
  word1 word2   - AND (both required)
  word1 | word2 - OR
  !word         - NOT
  (group) or <group> - grouping

PATH & LOCATION:
  path:folder   - must be in path containing "folder"
  nopath:word   - name only, ignore path
  parent:dir    - immediate parent directory matches
  D:            - on drive D:
  \\folder\\     - contains path segment

FILE TYPES:
  ext:pdf;docx  - extension match (semicolon separated)
  audio:        - mp3,wav,flac, etc.
  doc:          - pdf,doc,docx,xls,xlsx,ppt,pptx,txt,rtf
  exe:          - exe,msi,bat,cmd,ps1,com
  pic:          - jpg,png,gif,bmp,tif,webp,svg
  video:        - mp4,mkv,avi,mov,wmv,webm
  zip:          - zip,7z,rar,tar,gz,bz2,xz

SIZE & DATE:
  size:>10mb    - size greater than (supports b,kb,mb,gb,tb, empty,tiny,small,medium,large,huge)
  size:1mb..50mb - range
  dm:today      - date modified (today,yesterday,thisweek,thismonth,thisyear or YYYY-MM-DD)
  dm:>2025-01-01

REGEX & ADVANCED:
  regex:pattern - Python regex on full path (case-insensitive)
  noregex:word  - literal, no regex
  count:100     - limit results to 100

SPECIAL:
  file:         - files only
  folder:       - folders only
  "word"        - exact in name or path

TIPS FOR HIGHEST PRODUCTIVITY:
  - Use path: or nopath: or the Match Path checkbox for control
  - Combine: ext:pdf path:reports dm:thismonth size:>1mb
  - History: Down arrow or Ctrl+Space shows previous searches
  - Double-click Path column to open containing folder instantly
"""

        text.insert("1.0", help_content)
        text.configure(state=tk.DISABLED)

        ttk.Button(help_win, text="Close", command=help_win.destroy).pack(pady=5)

    def _on_close(self):
        """Save everything on exit for seamless restart (highest reliability)."""
        self._save_settings()
        self.root.destroy()

    def _setup_ui(self):
        # --- Row 1: Search bar (expands full width) ---
        search_row = ttk.Frame(self.root, padding=(5, 5, 5, 2))
        search_row.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(search_row, text="Search:").pack(side=tk.LEFT, padx=(0, 4))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        self.search_entry = ttk.Combobox(search_row, textvariable=self.search_var, font=("Segoe UI", 11))
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.search_entry.insert(0, SEARCH_PLACEHOLDER)
        self.search_entry.configure(foreground="gray")
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)
        self.search_entry.bind("<Return>", lambda _: self._run_search())
        self.search_entry.bind("<Down>", self._focus_first_result)
        self.search_entry.bind("<<ComboboxSelected>>", lambda e: self._run_search())
        self.search_entry.bind("<KeyRelease-Down>", self._show_search_history)

        # --- Row 2: Sort, options, and action buttons ---
        controls_row = ttk.Frame(self.root, padding=(5, 0, 5, 4))
        controls_row.pack(side=tk.TOP, fill=tk.X)

        sort_frame = ttk.Frame(controls_row)
        sort_frame.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(sort_frame, text="Sort:").pack(side=tk.LEFT)

        self.sort_var = tk.StringVar(value="Date")
        self.sort_combo = ttk.Combobox(
            sort_frame,
            textvariable=self.sort_var,
            values=("Date", "Name", "Size", "Path", "Ext", "Type"),
            width=9,
            state="readonly",
        )
        self.sort_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _: self._rerender_current_results())

        self.sort_desc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sort_frame,
            text="Desc",
            variable=self.sort_desc_var,
            command=self._rerender_current_results,
        ).pack(side=tk.LEFT)

        # Match Path toggle + Syntax Help
        match_frame = ttk.Frame(controls_row)
        match_frame.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            match_frame,
            text="Match Path",
            variable=self.match_path_var,
            command=self._on_match_path_changed,
        ).pack(side=tk.LEFT)
        ttk.Button(match_frame, text="Syntax Help", command=self._show_syntax_help).pack(side=tk.LEFT, padx=4)

        btn = ttk.Frame(controls_row)
        btn.pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Button(btn, text="Browse & Index...", command=self._pick_and_index).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="Re-index Default", command=self._manual_reindex_default).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="Refresh Now", command=lambda: self._start_index(self.default_roots, mode="refresh", interactive=False, reason="manual refresh")).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="Clear Index", command=self._clear_index).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="Index Info", command=self._show_index_info).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="Export CSV", command=self._export_csv).pack(side=tk.LEFT, padx=2)

        self.root.bind("<Escape>", lambda _: self._cancel_index())
        self.root.bind_all("<Control-u>", lambda e: self._toggle_match_path())
        self.root.bind_all("<F1>", lambda e: self._show_syntax_help())

        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(4, 0))

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("Name", "Ext", "Type", "Size", "Date", "Path"),
            show="headings",
            selectmode="browse",
        )
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        col_specs = [
            ("Name", 220, "w"),
            ("Ext", 70, "w"),
            ("Type", 80, "w"),
            ("Size", 95, "e"),
            ("Date", 150, "w"),
            ("Path", 560, "w"),
        ]
        for col, width, anchor in col_specs:
            self.tree.heading(col, text=col, anchor=anchor, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor)

        self.tree.tag_configure("row_white", background="#ffffff")
        self.tree.tag_configure("row_gray", background="#f1f1f1")

        # Keep selection highlight the same colour whether or not the tree has focus
        _style = ttk.Style()
        _style.map(
            "Treeview",
            background=[("selected", "#0078d7")],
            foreground=[("selected", "white")],
        )

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self._open_item)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Return>", self._open_item)
        self.tree.bind("<Control-c>", self._copy_path)

        status_frame = ttk.Frame(self.root, padding=(5, 2))
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.status_var = tk.StringVar(value=STATUS_IDLE)
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=180)
        # Progress bar is hidden until indexing starts; shown/hidden by _start_index/_poll_status

    # ------------------------------------------------------------------
    # Status and scheduler
    # ------------------------------------------------------------------
    def _set_status_base(self, text: str):
        self._status_base = text
        self._render_status()

    def _render_status(self):
        if self.is_indexing:
            self.status_var.set(self._status_base)
            return

        if self._next_refresh_epoch is None:
            self.status_var.set(self._status_base)
            return

        remain = max(0, int(self._next_refresh_epoch - time.time()))
        mm = remain // 60
        ss = remain % 60
        mp = "Path" if self.match_path_var.get() else "Name"
        self.status_var.set(f"{self._status_base} | Match: {mp} | Next refresh in {mm:02d}:{ss:02d} | Last refresh: {self._last_refresh_text}")

    def _start_scheduler(self):
        self._next_refresh_epoch = time.time() + AUTO_REFRESH_SECONDS
        self.root.after(1000, self._tick_scheduler)

    def _tick_scheduler(self):
        if (not self.is_indexing) and self._next_refresh_epoch and time.time() >= self._next_refresh_epoch:
            self._start_index(self.default_roots, mode="refresh", interactive=False, reason="scheduled refresh")
            self._next_refresh_epoch = time.time() + AUTO_REFRESH_SECONDS
        self._render_status()
        self.root.after(1000, self._tick_scheduler)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _focus_first_result(self, event):
        children = self.tree.get_children()
        if children:
            iid = children[0]
            self.tree.focus(iid)
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.tree.focus_set()
        return "break"

    def _search_focus_in(self, _event):
        if self.search_var.get() == SEARCH_PLACEHOLDER:
            self.search_var.set("")
            self.search_entry.configure(foreground="black")

    def _search_focus_out(self, _event):
        if not self.search_var.get():
            self.search_var.set(SEARCH_PLACEHOLDER)
            self.search_entry.configure(foreground="gray")

    def _on_search_changed(self):
        if hasattr(self, "_search_job"):
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(SEARCH_DEBOUNCE, self._run_search)

    def _run_search(self):
        text = self.search_var.get()
        if text == SEARCH_PLACEHOLDER or not text.strip():
            self._display_results([])
            self._set_status_base(STATUS_IDLE)
            return

        try:
            plan = self._query_engine.build(text.strip())
        except QuerySyntaxError as exc:
            self._set_status_base(f"Query syntax error: {exc}")
            self._display_results([])
            return
        except Exception as exc:
            self._set_status_base(f"Search error: {exc}")
            return

        self._add_to_search_history(text.strip())

        # Bump generation — any in-flight workers from prior keystrokes will
        # discard their results when they see the generation has moved on.
        self._search_gen += 1
        gen = self._search_gen
        t0 = time.perf_counter()
        limit = min(plan.get("limit", MAX_RESULTS), MAX_RESULTS)

        ram = self._ram_rows
        n = len(ram)
        if n == 0:
            self._display_results([])
            self._set_status_base(STATUS_IDLE)
            return

        self._search_pending_results[gen] = {
            "chunks_done": 0,
            "num_chunks": 1,
            "results": [],
            "limit": limit,
            "t0": t0,
            "total": n,
        }

        ast = plan.get("ast")
        # --- Fast paths: avoid full AST evaluation for common query shapes ---
        if (
            ast and ast[0] == "term"
            and ":" not in ast[1]
            and '"' not in ast[1]
            and not any(ch in ast[1] for ch in ("*", "?", "[", "\\", "/"))
        ):
            # Fast path 1: single plain keyword — substring in fullpath
            q = ast[1].lower()
            def _fast_worker(_ram, _gen, _limit):
                out = []
                for r in _ram:
                    if q in r.fullpath_lower:
                        out.append(r)
                        if len(out) >= _limit:
                            break
                return _gen, out
            f = self._search_executor.submit(_fast_worker, ram, gen, limit)
        elif (
            ast and ast[0] == "and"
            and ast[1][0] == "term" and ":" not in ast[1][1]
            and '"' not in ast[1][1]
            and not any(ch in ast[1][1] for ch in ("*", "?", "[", "\\", "/"))
            and ast[2][0] == "term" and ast[2][1].lower().startswith("ext:")
        ):
            # Fast path 2: keyword + ext:xxx — substring in fullpath and ext match
            q = ast[1][1].lower()
            ext_opts = {x.strip().lstrip(".").lower() for x in ast[2][1].split(":", 1)[1].split(";") if x.strip()}
            def _fast_ext_worker(_ram, _gen, _limit):
                out = []
                for r in _ram:
                    if r.ext_lower in ext_opts and q in r.fullpath_lower:
                        out.append(r)
                        if len(out) >= _limit:
                            break
                return _gen, out
            f = self._search_executor.submit(_fast_ext_worker, ram, gen, limit)
        else:
            engine = self._query_engine
            def _full_worker(_ram, _plan, _gen, _limit, _match_path):
                out = []
                evaluate = engine.evaluate
                for row in _ram:
                    if evaluate(_plan, row, _match_path):
                        out.append(row)
                        if len(out) >= _limit:
                            break
                return _gen, out
            f = self._search_executor.submit(_full_worker, ram, plan, gen, limit, self.match_path_var.get())

        f.add_done_callback(self._search_future_done)

    def _search_future_done(self, future):
        try:
            result_gen, partial = future.result()
        except Exception:
            return
        self._search_result_queue.put((result_gen, partial))

    def _poll_search_results(self):
        try:
            while True:
                result_gen, partial = self._search_result_queue.get_nowait()
                if result_gen != self._search_gen:
                    # Stale — discard
                    state = self._search_pending_results.get(result_gen)
                    if state:
                        state["chunks_done"] += 1
                        if state["chunks_done"] >= state["num_chunks"]:
                            del self._search_pending_results[result_gen]
                    continue

                state = self._search_pending_results.get(result_gen)
                if state is None:
                    continue

                state["chunks_done"] += 1
                # Accumulate up to limit
                remaining = state["limit"] - len(state["results"])
                if remaining > 0 and partial:
                    state["results"].extend(partial[:remaining])

                if state["chunks_done"] >= state["num_chunks"]:
                    elapsed_ms = (time.perf_counter() - state["t0"]) * 1000.0
                    results = state["results"]
                    total = state["total"]
                    del self._search_pending_results[result_gen]
                    self._display_results(results)
                    self._set_status_base(
                        f"{len(results):,} result(s) in {elapsed_ms:.0f} ms "
                        f"| RAM index: {total:,} items"
                    )
        except queue.Empty:
            pass
        self.root.after(50, self._poll_search_results)

    # ------------------------------------------------------------------
    # Results display and sorting
    # ------------------------------------------------------------------
    def _sort_rows(self, rows: list):
        field = self.sort_var.get()
        desc = bool(self.sort_desc_var.get())

        def key(r):
            if field == "Name":  return r.name_lower
            if field == "Size":  return (r.size or 0) if not r.is_dir else -1
            if field == "Date":  return r.mtime or 0
            if field == "Path":  return r.fullpath_lower
            if field == "Ext":   return r.ext_lower
            if field == "Type":  return "folder" if r.is_dir else "file"
            return r.mtime or 0

        return sorted(rows, key=key, reverse=desc)

    def _display_results(self, rows: list):
        sorted_rows = self._sort_rows(rows)

        self.tree.delete(*self.tree.get_children())
        self._row_data.clear()

        # Suppress per-row redraws: hide display columns during bulk insert,
        # then restore in one repaint.
        self.tree["displaycolumns"] = ()
        for idx, r in enumerate(sorted_rows):
            size_str = self._fmt_size(r.size) if not r.is_dir else "<DIR>"
            date_str = (
                datetime.datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
                if r.mtime else ""
            )
            ext_text = (r.ext or "").lstrip(".")
            typ = "Folder" if r.is_dir else "File"
            tag = "row_gray" if idx % 2 else "row_white"
            iid = self.tree.insert(
                "",
                tk.END,
                values=(r.name or "", ext_text, typ, size_str, date_str, r.fullpath or ""),
                tags=(tag,),
            )
            self._row_data[iid] = r
        self.tree["displaycolumns"] = ("Name", "Ext", "Type", "Size", "Date", "Path")

    def _rerender_current_results(self):
        rows = [self._row_data[iid] for iid in self.tree.get_children() if iid in self._row_data]
        self._display_results(rows)

    @staticmethod
    def _fmt_size(size) -> str:
        if size is None:
            return ""
        size = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def _sort_by(self, col: str):
        ascending = not self._sort_state.get(col, False)
        self._sort_state[col] = ascending

        self.sort_var.set(col)
        self.sort_desc_var.set(not ascending)

        self._rerender_current_results()

        arrow = " ▲" if ascending else " ▼"
        for c in ("Name", "Ext", "Type", "Size", "Date", "Path"):
            self.tree.heading(c, text=c + (arrow if c == col else ""))

    # ------------------------------------------------------------------
    # Open on double-click / right-click context menu
    # ------------------------------------------------------------------
    def _open_item(self, event=None):
        """Double-click handler: if on Path column, open containing folder (Everything parity, highest navigation impact)."""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        r = self._row_data.get(iid)
        if r is None:
            return

        if event:
            col = self.tree.identify_column(event.x)
            if col == "#6":  # Path column (columns: #1 Name, #2 Ext, #3 Type, #4 Size, #5 Date, #6 Path)
                try:
                    folder = os.path.dirname(r.fullpath)
                    if folder and os.path.exists(folder):
                        os.startfile(folder)
                        return
                except Exception as exc:
                    self._set_status_base(f"Cannot open folder: {exc}")
                    return

        # Default: open the item itself
        try:
            os.startfile(r.fullpath)
        except Exception as exc:
            self._set_status_base(f"Cannot open item: {exc}")

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.focus_set()
        r = self._row_data.get(iid)
        if r is None:
            return
        fullpath = r.fullpath
        if not fullpath:
            return

        def _open():
            try:
                os.startfile(fullpath)
            except Exception as exc:
                self._set_status_base(f"Cannot open item: {exc}")

        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Open", command=_open)
        menu.add_command(label="Copy Path", command=lambda: self._copy_path_text(fullpath))
        menu.add_separator()
        menu.add_command(
            label="Shell Menu…",
            command=lambda: self._show_shell_menu(fullpath, event.x_root, event.y_root),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        self.tree.focus_set()

    def _copy_path_text(self, fullpath: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(fullpath)
        self._set_status_base(f"Copied: {fullpath}")

    def _copy_path(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return "break"
        r = self._row_data.get(sel[0])
        if r is None:
            return "break"
        self._copy_path_text(r.fullpath)
        return "break"

    def _show_shell_menu(self, fullpath: str, x: int, y: int):
        if not Path(fullpath).exists():
            self._set_status_base(f"File no longer exists: {fullpath}")
            return
        hwnd = self.root.winfo_id()
        ShellContextMenu().show(hwnd, fullpath, x, y)
        self.tree.focus_set()

    def _show_index_info(self):
        if self.db is None:
            messagebox.showinfo("Index Info", "No database loaded.")
            return
        count = self.db.get_file_count()
        version = self.db.get_meta("index_version", "\u2014")
        roots = self.db.get_meta("indexed_roots", "\u2014")
        last = self.db.get_meta("last_indexed", "\u2014")
        state = self.db.get_meta("build_state", "\u2014")
        roots_fmt = (roots or "\u2014").replace("|", "\n  ")
        msg = (
            f"Records:       {count:,}\n"
            f"Index version: {version}\n"
            f"Build state:   {state}\n"
            f"Last indexed:  {last}\n"
            f"Indexed roots:\n  {roots_fmt}"
        )
        messagebox.showinfo("Index Info", msg)

    def _export_csv(self):
        rows = [self._row_data[iid] for iid in self.tree.get_children() if iid in self._row_data]
        if not rows:
            self._set_status_base("No results to export.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Export results to CSV",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "Extension", "Type", "Size", "Date Modified", "Full Path"])
                for r in rows:
                    size_str = self._fmt_size(r.size) if not r.is_dir else "<DIR>"
                    date_str = (
                        datetime.datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
                        if r.mtime else ""
                    )
                    writer.writerow([
                        r.name,
                        (r.ext or "").lstrip("."),
                        "Folder" if r.is_dir else "File",
                        size_str,
                        date_str,
                        r.fullpath,
                    ])
            self._set_status_base(f"Exported {len(rows):,} rows to {path}")
        except OSError as exc:
            self._set_status_base(f"Export failed: {exc}")

    # ------------------------------------------------------------------
    # DB and RAM cache
    # ------------------------------------------------------------------
    def _load_db(self):
        db_path = os.path.join(os.path.expanduser("~"), DB_NAME)
        self.db = FileIndexDB(db_path)

    def _load_ram_cache(self):
        if self.db is None:
            self._ram_rows = []
            return
        self._ram_rows = self.db.get_all_rows()

        count = len(self._ram_rows)
        if count:
            stored_ver = self.db.get_meta("index_version")
            if stored_ver and stored_ver != INDEX_VERSION:
                self._set_status_base(
                    f"Loaded cached index: {count:,} items  "
                    f"[index version mismatch v{stored_ver}→v{INDEX_VERSION}; consider Re-indexing]"
                )
            else:
                self._set_status_base(f"Loaded cached index: {count:,} items")
        else:
            self._set_status_base("No cached index found. Building startup index...")

    # ------------------------------------------------------------------
    # Indexing lifecycle
    # ------------------------------------------------------------------
    def _startup_index_flow(self):
        if not self.default_roots:
            self._set_status_base("No startup roots found (D:\\ and user profile are unavailable)")
            return

        cached_count = self.db.get_file_count() if self.db else 0
        mode = "refresh" if cached_count > 0 else "rebuild"
        reason = "startup cache refresh" if cached_count > 0 else "startup initial indexing"
        self._start_index(self.default_roots, mode=mode, interactive=False, reason=reason)

    def _pick_and_index(self):
        path = filedialog.askdirectory(initialdir=self.default_roots[0] if self.default_roots else "C:\\", title="Select folder to index")
        if path:
            self._start_index([path], mode="rebuild", interactive=True, reason="manual folder index")

    def _manual_reindex_default(self):
        if not self.default_roots:
            self._set_status_base("No default roots available for reindex")
            return
        self._start_index(self.default_roots, mode="rebuild", interactive=True, reason="manual default reindex")

    def _start_index(self, roots: list, mode: str, interactive: bool, reason: str):
        valid_roots = [str(Path(r)) for r in roots if Path(r).exists()]
        if not valid_roots:
            self._set_status_base("No valid roots selected for indexing")
            return

        if self.is_indexing:
            self._set_status_base("Indexing already in progress")
            return

        if interactive:
            if not messagebox.askyesno(
                "Confirm Index",
                "Index these roots:\n\n" + "\n".join(valid_roots) + "\n\nThis can take several minutes.",
            ):
                return

        self.is_indexing = True
        self.indexer = FileIndexer(self.db, valid_roots, mode=mode)

        self.progress.pack(side=tk.RIGHT)
        self.progress.start(8)
        self._set_status_base(f"{reason}: starting...")

        threading.Thread(target=self._indexer_thread, daemon=True, name="idx-runner").start()

    def _indexer_thread(self):
        try:
            self.indexer.run()
        except Exception as exc:
            self.indexer.status_queue.put(("error", str(exc)))

    def _cancel_index(self):
        if self.is_indexing and self.indexer:
            self.indexer.cancel()
            self._set_status_base("Cancelling index update...")

    def _clear_index(self):
        if self.is_indexing:
            self._set_status_base("Cannot clear while indexing")
            return

        if not messagebox.askyesno("Confirm", "Delete all indexed data?"):
            return

        self.db.clear()
        self._ram_rows = []
        self.tree.delete(*self.tree.get_children())
        self._row_data.clear()
        self.db.update_index_metadata(self.default_roots, "cleared")
        self._set_status_base("Index cleared")

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------
    def _poll_status(self):
        if self.indexer is not None:
            try:
                while True:
                    msg_type, payload = self.indexer.status_queue.get_nowait()

                    if msg_type == "phase":
                        self._set_status_base(f"Updating index... {payload}")

                    elif msg_type == "progress":
                        self._set_status_base(f"Updating index... {payload:,} records written")

                    elif msg_type == "done":
                        self.is_indexing = False
                        self.progress.stop()
                        self.progress.pack_forget()
                        self._last_refresh_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.db.update_index_metadata(self.default_roots, "ready")
                        self._load_ram_cache()
                        self._set_status_base(payload)

                    elif msg_type == "error":
                        self.is_indexing = False
                        self.progress.stop()
                        self.progress.pack_forget()
                        self._set_status_base(f"Indexing error: {payload}")
            except queue.Empty:
                pass

        self.root.after(100, self._poll_status)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    root = tk.Tk()
    root.withdraw()
    EverythingGUIApp(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
