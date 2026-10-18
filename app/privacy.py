"""Keep the private reading queue out of the public repo.

`config/sources.yaml` is git-ignored because the subscription list is private,
but the TickTick queue leaked anyway — real item titles were used as example
data in TODO.md, `app/ticktick.py` and `tests/test_ticktick.py`, and named in
two commit messages.

Nothing private is written down in this file. The phrases are read from the
LOCAL, git-ignored queue at call time, so the guard can live in a public repo
without republishing what it guards.
"""
from __future__ import annotations

import re
import subprocess

# Below this a title matches ordinary prose ("Book", "AI", "read") and the
# guard becomes noise everyone learns to ignore.
MIN_PHRASE_CHARS = 10
MIN_TOKEN_CHARS = 12


def private_phrases(titles, min_chars: int = MIN_PHRASE_CHARS) -> list[str]:
    """Distinctive phrases worth grepping for, longest first.

    Longest first so an overlapping match is reported at its most specific.
    """
    out: set[str] = set()
    for raw in titles:
        title = " ".join((raw or "").split())
        # Several words together are distinctive; one long word usually is not.
        # "instructions" is 12 characters and flagged two source files.
        if len(title) >= min_chars and len(title.split()) >= 2:
            out.add(title.lower())
        for tok in re.findall(rf"[A-Za-z0-9][\w.-]{{{MIN_TOKEN_CHARS - 1},}}", title):
            if _looks_like_identifier(tok):
                out.add(tok.lower())
    return sorted(out, key=len, reverse=True)


def _looks_like_identifier(token: str) -> bool:
    """True for a domain, slug or code-ish name — distinctive standing alone.

    A run of plain lowercase letters is an ordinary English word however long
    it is, so it is not enough on its own.
    """
    return bool(re.search(r"[.\-_0-9]", token) or re.search(r"[a-z][A-Z]", token))


def scan_text(text: str, phrases) -> list[tuple[int, str]]:
    """[(line number, phrase)] for every private phrase appearing in `text`."""
    hits: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        low = " ".join(line.split()).lower()
        for phrase in phrases:
            if phrase in low:
                hits.append((n, phrase))
                break  # one report per line is enough to act on
    return hits


def _git(*args: str) -> str:
    # errors="ignore", as for tracked files: a staged binary (an MP3, an
    # image) is not UTF-8, and a crash here blocked the commit outright.
    return subprocess.run(("git", *args), capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", check=False).stdout


def local_private_phrases() -> list[str]:
    """Phrases from the local queue. Empty when the private data is absent
    (CI, a fresh clone) — the guard is meant to run where the secrets are."""
    try:
        from sqlmodel import select

        from . import db
        from .db import TickTickItem
    except Exception:
        return []
    try:
        with db.session() as s:
            titles = list(s.exec(select(TickTickItem.title)).all())
    except Exception:
        return []
    return private_phrases(titles)


def scan_tracked_files(phrases, paths=None) -> list[str]:
    """['path:line: phrase', …] for tracked files quoting a private phrase."""
    if not phrases:
        return []
    files = paths or [f for f in _git("ls-files").splitlines() if f]
    findings = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (OSError, IsADirectoryError):
            continue
        findings += [f"{path}:{n}: {phrase}" for n, phrase in scan_text(text, phrases)]
    return findings


def scan_staged(phrases) -> list[str]:
    """Same, but against what is about to be committed."""
    if not phrases:
        return []
    staged = [f for f in _git("diff", "--cached", "--name-only").splitlines() if f]
    findings = []
    for path in staged:
        blob = _git("show", f":{path}")
        findings += [f"{path}:{n}: {phrase}" for n, phrase in scan_text(blob, phrases)]
    return findings


def scan_message_file(path: str, phrases) -> list[str]:
    """Scan a commit message about to be written (the commit-msg hook's arg).

    Pre-commit only sees staged file content, so a private phrase quoted in the
    MESSAGE went straight through — commit 4ab022a did exactly that while
    describing this guard.

    git's own comment lines are skipped: the template, and with --verbose the
    entire staged diff, arrive in this same file.
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            body = "\n".join(ln for ln in fh.read().splitlines()
                              if not ln.lstrip().startswith("#"))
    except OSError:
        return []
    return [f"commit message:{n}: {phrase}" for n, phrase in scan_text(body, phrases)]


def scan_commit_messages(phrases, rev_range: str = "HEAD") -> list[str]:
    if not phrases:
        return []
    findings = []
    for rev in _git("rev-list", rev_range).split():
        msg = _git("log", "-1", "--pretty=%B", rev)
        findings += [f"{rev[:9]} (message): {phrase}" for _, phrase in scan_text(msg, phrases)]
    return findings
