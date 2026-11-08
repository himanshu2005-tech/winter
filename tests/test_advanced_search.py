from providers import research_api


def test_advanced_search_deduplicates_and_caches(monkeypatch):
    research_api._cache.clear()
    monkeypatch.setenv("TAVILY_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "y")
    calls = {"tavily": 0, "google": 0}

    def tavily(*args, **kwargs):
        calls["tavily"] += 1
        return {"answer": "grounded", "providers": ["tavily"], "results": [
            {"title": "Official", "url": "https://example.gov/page?utm_source=x",
             "content": "primary", "score": .7, "provider": "tavily"}]}

    def google(*args, **kwargs):
        calls["google"] += 1
        return {"answer": "", "providers": ["google"], "results": [
            {"title": "Official duplicate", "url": "https://example.gov/page",
             "content": "snippet", "score": .9, "provider": "google"},
            {"title": "Other", "url": "https://example.com/other",
             "content": "other", "score": .8, "provider": "google"}]}

    monkeypatch.setattr(research_api, "search", tavily)
    monkeypatch.setattr(research_api, "_google_search", google)
    first = research_api.advanced_search("test query", provider="all")
    second = research_api.advanced_search("test query", provider="all")
    assert len(first["results"]) == 2
    assert set(first["providers"]) == {"google", "tavily"}
    assert second["cache_hit"] is True
    assert calls == {"tavily": 1, "google": 1}


def test_auto_falls_back_to_google(monkeypatch):
    research_api._cache.clear()
    monkeypatch.setenv("SERPAPI_API_KEY", "y")
    monkeypatch.setattr(research_api, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(research_api, "_google_search",
                        lambda *a, **k: {"answer": "ok", "providers": ["google"],
                                         "results": []})
    result = research_api.advanced_search("fallback query")
    assert result["answer"] == "ok"
    assert result["provider_warnings"] == ["offline"]
