"""Probe: is the substack.com cookie in config/secrets.yaml still a live paid session?

Fetches known-paid posts through the same code path as ingest and prints one
line per probe plus a final COOKIE-OK / COOKIE-EXPIRED verdict (exit 0 / 1).
Run from the repo root: .venv/bin/python scripts/check_substack_access.py

Background: the session cookie silently expired ~2026-07-17 and paid posts
published as previews for four days (ep. 243 feedback). This probe catches the
next expiry without waiting for a bad episode.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.substack import fetch_post  # noqa: E402

# Known paid posts (audience=only_paid) on publications the configured account
# has an ACTIVE paid subscription to — verify entitlement with
# {sub}.substack.com/api/v1/subscription (200 + status active), NOT the profile
# list's membership_state, before adding a probe here. Probing a pub without an
# active sub reports a false COOKIE-EXPIRED (2026-07-23 lesson: matthewyglesias
# was free_signup all along, noahpinion's paid sub lives on another account).
PROBES = [
    ("phillipspobrien", "bravery-does-not-win-wars"),
]


async def main() -> int:
    ok = True
    for sub, slug in PROBES:
        post = await fetch_post(sub, slug)
        if post is None:
            print(f"{sub}/{slug}: API fetch FAILED")
            ok = False
            continue
        verdict = "FULL" if post["accessible"] else "TRUNCATED"
        print(f"{sub}/{slug}: {verdict} "
              f"({post['delivered_words']}/{post['wordcount']} words)")
        ok = ok and post["accessible"]
    print("COOKIE-OK" if ok else "COOKIE-EXPIRED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
