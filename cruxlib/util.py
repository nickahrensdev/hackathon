"""Small shared helpers: token estimates, hashing, JSON IO, logging, text utilities."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

# Code tokenizes denser than prose; ~3.5 chars/token is a slightly pessimistic
# average for Claude models on source code, which keeps budgets on the safe side.
CHARS_PER_TOKEN = 3.5

_VERBOSE = True


def set_verbose(v: bool) -> None:
    global _VERBOSE
    _VERBOSE = v


def log(msg: str) -> None:
    if _VERBOSE:
        sys.stderr.write(f"[crux {time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"[crux WARN] {msg}\n")
    sys.stderr.flush()


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36(data: bytes, n: int) -> str:
    v = int.from_bytes(data, "big")
    out = []
    for _ in range(n):
        v, r = divmod(v, 36)
        out.append(_B36[r])
    return "".join(out)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: str, obj, indent: int | None = 1) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_text(path: str, max_bytes: int | None = None) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes) if max_bytes else f.read()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", "replace")


def one_line(text: str, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def clamp_int(v, lo: int, hi: int, default: int | None = None):
    try:
        iv = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def extract_json_object(text: str):
    """Pull the first top-level JSON object out of model text (fences, prose tolerated)."""
    if not text:
        raise ValueError("empty response")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("unterminated JSON object")
