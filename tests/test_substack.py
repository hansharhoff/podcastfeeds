"""Truncation detection for the Substack post API (ep. 243 feedback).

A logged-out / expired session still gets HTTP 200 with a truncated body_html
that carries no paywall CTA, so `is_paywalled` alone misses it. The API's
`wordcount` field reports the FULL post length — comparing it against the
words actually delivered is the reliable signal (observed 2026-07-21:
slowboring delivered ~60 words vs wordcount 1301; noahpinion ~1300 vs 3878).
"""
import asyncio

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


# ── by-id host routing: entitlement honoring is host-dependent — the
#    publication subdomain honors web/Stripe-billed subs only; the
#    substack.com host also honors reader-app-billed ones (noahpinion,
#    2026-07-24). Billing type is not detectable up front, so fetch_post
#    consults substack.com/api/v1/posts/by-id/{id} for EVERY paid post
#    (routing by `audience`, never by the truncation heuristic) and the
#    fuller delivered body wins. ──

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_api(subdomain_json, by_id_json, calls=None):
    async def fake_api(url, cookie_url=None):
        if calls is not None:
            calls.append(url)
        if "/posts/by-id/" in url:
            return by_id_json
        return subdomain_json

    return fake_api


def test_fetch_post_uses_by_id_for_truncated_paid_body(monkeypatch):
    from app import substack
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(1261),
         "audience": "only_paid", "wordcount": 3878, "id": 99},
        {"post": {"title": "t", "body_html": _html(3900),
                  "audience": "only_paid", "wordcount": 3878, "id": 99}}))
    post = _run(substack.fetch_post("noahpinion", "some-post"))
    assert post["accessible"] is True
    assert post["delivered_words"] == 3900


def test_fetch_post_keeps_truncated_when_by_id_also_truncated(monkeypatch):
    from app import substack
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(1261),
         "audience": "only_paid", "wordcount": 3878, "id": 99},
        {"post": {"title": "t", "body_html": _html(1261),
                  "audience": "only_paid", "wordcount": 3878, "id": 99}}))
    post = _run(substack.fetch_post("noahpinion", "some-post"))
    assert post["accessible"] is False


def test_fetch_post_consults_by_id_even_when_subdomain_body_is_full(monkeypatch):
    # Routing must not depend on the truncation verdict: a full subdomain
    # body still consults the by-id host, and stays the result when by-id
    # is not fuller (e.g. a per-publication session the substack.com host
    # cookie does not carry).
    from app import substack
    calls: list = []
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(1290),
         "audience": "only_paid", "wordcount": 1301, "id": 99},
        {"post": {"title": "t", "body_html": _html(60),
                  "audience": "only_paid", "wordcount": 1301, "id": 99}},
        calls))
    post = _run(substack.fetch_post("phillipspobrien", "some-post"))
    assert post["accessible"] is True
    assert post["delivered_words"] == 1290
    assert any("/posts/by-id/99" in url for url in calls)


def test_fetch_post_by_id_wins_when_missing_wordcount_hides_truncation(monkeypatch):
    # The pre-2026-07-25 trigger gap: no wordcount + no CTA judged a
    # truncated preview accessible, so the by-id host was never consulted.
    # audience-based routing fetches it anyway and the fuller body wins.
    from app import substack
    monkeypatch.setattr(substack, "is_paywalled", lambda body, html: False)
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(600),
         "audience": "only_paid", "id": 99},
        {"post": {"title": "t", "body_html": _html(2000),
                  "audience": "only_paid", "id": 99}}))
    post = _run(substack.fetch_post("noahpinion", "some-post"))
    assert post["delivered_words"] == 2000


def test_fetch_post_free_post_never_consults_by_id(monkeypatch):
    from app import substack
    calls: list = []
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(500),
         "audience": "everyone", "wordcount": 500, "id": 99},
        None, calls))
    post = _run(substack.fetch_post("phillipspobrien", "some-post"))
    assert post["accessible"] is True
    assert not any("/posts/by-id/" in url for url in calls)


def test_fetch_post_survives_by_id_request_failure(monkeypatch):
    from app import substack
    monkeypatch.setattr(substack, "_api_json", _fake_api(
        {"title": "t", "body_html": _html(1290),
         "audience": "only_paid", "wordcount": 1301, "id": 99},
        None))
    post = _run(substack.fetch_post("phillipspobrien", "some-post"))
    assert post["accessible"] is True
    assert post["delivered_words"] == 1290


# ── ep 312 defer-loop (2026-07-25): a FULL paid body that happens to embed a
#    subscribe-CTA block tripped is_paywalled and deferred forever. When the
#    API provides wordcount, it alone decides; the text heuristic is only a
#    fallback for responses without wordcount. ──

def test_full_paid_body_with_embedded_cta_is_accessible(monkeypatch):
    from app import substack
    monkeypatch.setattr(substack, "is_paywalled", lambda body, html: True)
    post = substack.post_from_api({"title": "t", "body_html": _html(1866),
                                   "audience": "only_paid", "wordcount": 1830})
    assert post["accessible"] is True


def test_no_wordcount_still_falls_back_to_paywall_text(monkeypatch):
    from app import substack
    monkeypatch.setattr(substack, "is_paywalled", lambda body, html: True)
    post = substack.post_from_api({"title": "t", "body_html": _html(500),
                                   "audience": "only_paid"})
    assert post["accessible"] is False


# ── a link followed OUT of a social post into Substack must use the post API,
#    not the generic fetch: the cookie-aware path is the only one that returns
#    the full body of a paid essay (found reviewing the ep-253 link-following
#    fix, 2026-07-28; a tweet pointing at a paid post came back as a preview). ──

def test_substack_ref_from_url_matches_post_urls():
    from app.substack import substack_ref_from_url
    assert substack_ref_from_url(
        "https://noahpinion.substack.com/p/some-essay") == ("noahpinion", "some-essay")
    assert substack_ref_from_url(
        "https://www.noahpinion.substack.com/p/some-essay/") == ("noahpinion", "some-essay")


def test_substack_ref_from_url_ignores_non_posts_and_other_hosts():
    from app.substack import substack_ref_from_url
    assert substack_ref_from_url("https://noahpinion.substack.com/archive") is None
    assert substack_ref_from_url("https://www.slowboring.com/p/essay") is None
    assert substack_ref_from_url("https://substack.com/p/essay") is None
    assert substack_ref_from_url("not a url") is None
