"""Truncation detection for the Substack post API (ep. 243 feedback).

A logged-out / expired session still gets HTTP 200 with a truncated body_html
that carries no paywall CTA, so `is_paywalled` alone misses it. The API's
`wordcount` field reports the FULL post length — comparing it against the
words actually delivered is the reliable signal (observed 2026-07-21:
slowboring delivered ~60 words vs wordcount 1301; noahpinion ~1300 vs 3878).
"""
from app.substack import _delivered_words, post_from_api


def _html(words: int) -> str:
    return "<p>" + " ".join(f"w{i}" for i in range(words)) + "</p>"


def test_delivered_words_counts_text_not_markup():
    assert _delivered_words("<p>one two</p><div>three</div>") == 3
    assert _delivered_words("") == 0


def test_free_post_is_accessible_regardless_of_wordcount():
    post = post_from_api({"title": "t", "body_html": _html(50),
                          "audience": "everyone", "wordcount": 900})
    assert post["accessible"] is True


def test_paid_post_truncated_body_is_not_accessible():
    # ep-243 shape: paid post, ~34% of the full wordcount delivered, no CTA.
    post = post_from_api({"title": "t", "body_html": _html(1300),
                          "audience": "only_paid", "wordcount": 3878})
    assert post["accessible"] is False


def test_paid_post_stub_is_not_accessible():
    # slowboring ep-267 shape: 60 words delivered vs wordcount 1301.
    post = post_from_api({"title": "t", "body_html": _html(60),
                          "audience": "only_paid", "wordcount": 1301})
    assert post["accessible"] is False


def test_paid_post_full_body_is_accessible():
    post = post_from_api({"title": "t", "body_html": _html(1250),
                          "audience": "only_paid", "wordcount": 1301})
    assert post["accessible"] is True


def test_paid_post_without_wordcount_falls_back_to_paywall_text():
    # No wordcount signal and no CTA in the body -> treated as accessible
    # (the pre-existing behaviour for API responses lacking wordcount).
    post = post_from_api({"title": "t", "body_html": _html(500),
                          "audience": "only_paid"})
    assert post["accessible"] is True


def test_post_from_api_carries_wordcount_for_provenance():
    post = post_from_api({"title": "t", "body_html": _html(60),
                          "audience": "only_paid", "wordcount": 1301})
    assert post["wordcount"] == 1301
    assert post["delivered_words"] == 60
