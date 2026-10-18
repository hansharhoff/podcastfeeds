"""The reading queue is private; the repo is public.

TickTick queue titles leaked into TODO.md, app/ticktick.py and
tests/test_ticktick.py as example data, and into two commit messages — still
present in HEAD as of 2026-09-20. This guards the class of mistake rather than
the specific strings: the phrases are read from the LOCAL, git-ignored queue at
runtime, so nothing private is written down here.
"""
from app.privacy import private_phrases, scan_text


def test_distinctive_title_is_found_in_a_file():
    phrases = private_phrases(["some obscure reading item"])
    hits = scan_text("a line\nmentioning Some Obscure Reading Item here\n", phrases)
    assert [h[0] for h in hits] == [2]                    # line number
    assert hits[0][1] == "some obscure reading item"


def test_matching_ignores_case_and_extra_whitespace():
    phrases = private_phrases(["  Widget   Assembly   Guide  "])
    assert scan_text("see the widget assembly guide", phrases)


def test_short_or_generic_titles_are_not_matched_on():
    # "Book", "AI", "Notes" would hit ordinary prose in every file and make the
    # guard useless; they are dropped rather than turned into noise.
    assert private_phrases(["Book", "AI", "Notes", "read"]) == []
    assert scan_text("this is a book about AI and my notes", private_phrases(["Book"])) == []


def test_a_long_single_token_is_matched_even_inside_other_text():
    # A bare domain from a queue item is distinctive on its own.
    phrases = private_phrases(["someverylongdomainname.example"])
    assert scan_text("url = 'https://someverylongdomainname.example/x'", phrases)


def test_clean_file_reports_nothing():
    phrases = private_phrases(["some obscure reading item"])
    assert scan_text("nothing to see here\njust code\n", phrases) == []


# The real scan against the live queue is NOT a pytest test: conftest.py
# redirects DATA_DIR to a throwaway dir, so it could only ever skip. It runs
# from scripts/check_private_leaks.py, wired in as a pre-commit hook.


def test_ordinary_long_words_are_not_treated_as_private():
    # "instructions" is 12 chars and appeared in a queue title, so a naive
    # long-token rule flagged app/summarize.py and app/ticktick.py. A guard
    # with false positives is a guard everyone learns to --no-verify past.
    assert private_phrases(["instructions"]) == []
    assert private_phrases(["documentation"]) == []


def test_domains_and_identifiers_still_count_as_private():
    # These are distinctive even standing alone.
    assert private_phrases(["invented-example-site.test"])
    assert private_phrases(["some-project-v2"])
    assert private_phrases(["ISO27001audit"])


def test_single_common_word_title_is_dropped_but_a_phrase_is_kept():
    assert private_phrases(["conversations"]) == []
    assert private_phrases(["conversations with a friend"])


def test_commit_message_scanning_ignores_gits_comment_lines(tmp_path):
    # git hands commit-msg a file that also holds the commented template (and,
    # with --verbose, the whole staged diff). Scanning those would block on
    # code the author is legitimately removing.
    from app.privacy import scan_message_file

    phrases = private_phrases(["some obscure reading item"])
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Fix the thing\n\n# On branch main\n"
                   "# some obscure reading item was here\n")
    assert scan_message_file(str(msg), phrases) == []


def test_commit_message_scanning_catches_a_private_phrase(tmp_path):
    # The hole this closes: 4ab022a quoted a real queue phrase in its own
    # message while describing the guard, and pre-commit waved it through
    # because it only scans staged file content.
    from app.privacy import scan_message_file

    phrases = private_phrases(["some obscure reading item"])
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Explain the guard\n\nverified against some obscure reading item\n")
    hits = scan_message_file(str(msg), phrases)
    assert len(hits) == 1 and "some obscure reading item" in hits[0]


def test_a_staged_binary_file_does_not_crash_the_scan(tmp_path, monkeypatch):
    import subprocess

    from app.privacy import scan_staged

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    (tmp_path / "tone.mp3").write_bytes(b"ID3\x03\x00\xff\xfb\x90\x00" * 50)
    (tmp_path / "notes.txt").write_text("mentions the secret phrase here\n")
    subprocess.run(["git", "add", "tone.mp3", "notes.txt"], check=True)
    assert scan_staged(["secret phrase"]) == ["notes.txt:1: secret phrase"]
