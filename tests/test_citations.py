"""Unit tests for citation / relevance / honesty helpers."""

from datetime import date, timedelta

from app.citations import (
    filter_out_concierge_buttons,
    filter_relevant_sources,
    format_sources_list_html,
    freshness_honesty_note,
    significant_terms,
    topic_needs_freshness,
)


def test_significant_terms_drops_stopwords():
    terms = significant_terms("какие новости сегодня про биткоин")
    assert "биткоин" in terms
    assert "какие" not in terms


def test_filter_relevant_sources_drops_no_overlap():
    sources = [
        {
            "title": "Bitcoin hits new high",
            "url": "https://example.com/btc",
            "snippet": "crypto market bitcoin rally",
            "published_at": "2026-08-06",
        },
        {
            "title": "Рецепт борща",
            "url": "https://example.com/soup",
            "snippet": "свекла и капуста",
            "published_at": "2026-08-05",
        },
    ]
    kept = filter_relevant_sources("bitcoin news today", sources)
    assert len(kept) == 1
    assert "btc" in kept[0]["url"]
    kept_ru = filter_relevant_sources("новости борщ рецепт", sources)
    assert len(kept_ru) == 1
    assert "soup" in kept_ru[0]["url"]


def test_format_sources_list_html_has_anchors():
    html = format_sources_list_html(
        [
            {
                "title": "Title A",
                "url": "https://example.com/a",
                "snippet": "x",
                "published_at": "",
            }
        ]
    )
    assert 'href="https://example.com/a"' in html
    assert "<b>Title A</b>" in html


def test_honesty_when_no_dates():
    note = freshness_honesty_note(
        "какие новости сегодня",
        [{"title": "x", "url": "https://e.com", "snippet": "y", "published_at": ""}],
        days=2,
        today=date(2026, 8, 6),
    )
    assert note and ("датированных" in note.lower() or "⚠️" in note)


def test_honesty_when_stale():
    old = (date(2026, 8, 6) - timedelta(days=10)).isoformat()
    note = freshness_honesty_note(
        "новости сегодня",
        [
            {
                "title": "Old",
                "url": "https://e.com/o",
                "snippet": "news",
                "published_at": old,
            }
        ],
        days=2,
        today=date(2026, 8, 6),
    )
    assert "⚠️" in note
    assert old in note


def test_hide_concierge_buttons():
    buttons = [
        {"text": "ok", "callback_data": "web_search"},
        {"text": "conc", "callback_data": "concierge_run"},
        {"text": "src", "url": "https://example.com"},
    ]
    kept = filter_out_concierge_buttons(buttons, concierge_enabled=False)
    assert len(kept) == 2
    assert kept[0]["callback_data"] == "web_search"
    assert kept[1]["url"] == "https://example.com"


def test_topic_needs_freshness():
    assert topic_needs_freshness("какие новости сегодня") is True
    assert topic_needs_freshness("расскажи анекдот") is False
