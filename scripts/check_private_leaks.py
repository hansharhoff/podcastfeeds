#!/usr/bin/env python3
"""Refuse to publish the private reading queue.

The repo is public; the TickTick queue is not. Real queue titles leaked into
TODO.md, app/ticktick.py, tests/test_ticktick.py and two commit messages.

Reads the phrases from the LOCAL queue at runtime — this file names none of
them, so it is safe to track.

  scripts/check_private_leaks.py            # staged changes (pre-commit hook)
  scripts/check_private_leaks.py --tracked  # every tracked file
  scripts/check_private_leaks.py --history  # every commit message (audit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.privacy import (  # noqa: E402
    local_private_phrases,
    scan_commit_messages,
    scan_staged,
    scan_tracked_files,
)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--staged"
    phrases = local_private_phrases()
    if not phrases:
        print("check-private-leaks: no local queue data; nothing to check against")
        return 0
    if mode == "--tracked":
        findings = scan_tracked_files(phrases)
    elif mode == "--history":
        findings = scan_commit_messages(phrases) + scan_tracked_files(phrases)
    else:
        findings = scan_staged(phrases)
    if not findings:
        print(f"check-private-leaks: clean ({len(phrases)} private phrases checked)")
        return 0
    print(f"check-private-leaks: BLOCKED — {len(findings)} leak(s) of the private "
          f"reading queue:", file=sys.stderr)
    for f in findings[:20]:
        print(f"  {f}", file=sys.stderr)
    if len(findings) > 20:
        print(f"  … and {len(findings) - 20} more", file=sys.stderr)
    print("\nUse invented example data instead. To override once: "
          "git commit --no-verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
