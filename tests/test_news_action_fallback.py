import tempfile
from queue import Queue
from unittest import mock

import actions


def _empty_news():
    return {"query": "news today", "sources": ["google_news"],
            "selection": "broad_current_news", "results": [],
            "errors": {}, "retrieved_at": "2026-10-26T12:00:00+00:00"}


def test_empty_free_feeds_fall_back_to_existing_web_search():
    fallback = {"query": "news today", "results": [
        {"title": "Current headline", "url": "https://news.example/story",
         "content": "Verified current report", "score": .9}],
        "answer": "", "providers": ["tavily"]}
    with tempfile.TemporaryDirectory() as folder, \
            mock.patch("providers.knowledge_api.search",
                       return_value=_empty_news()), \
            mock.patch("providers.research_api.advanced_search",
                       return_value=fallback) as web, \
            mock.patch("providers.research_api.format_for_model",
                       return_value="Current headline\nhttps://news.example/story"):
        actions.execute_actions(
            '<ACTION type="TRENDING_NEWS" query="news today" limit="6" />',
            {}, Queue(), folder, origin="user")
    web.assert_called_once()
    assert "News retrieval fallback" in actions.execute_actions._last_output
    assert "Current headline" in actions.execute_actions._last_output


def test_total_news_outage_is_honest_and_does_not_erase_source_diagnostics():
    empty = _empty_news()
    empty["errors"] = {"google_news": "offline"}
    with tempfile.TemporaryDirectory() as folder, \
            mock.patch("providers.knowledge_api.search", return_value=empty), \
            mock.patch("providers.research_api.advanced_search",
                       side_effect=RuntimeError("provider unavailable")):
        actions.execute_actions(
            '<ACTION type="TRENDING_NEWS" query="news today" limit="6" />',
            {}, Queue(), folder, origin="user")
    output = actions.execute_actions._last_output
    assert "No matching results" in output
    assert "google_news" in output
    assert "Fallback unavailable" in output
