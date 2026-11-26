import queue
from unittest import mock

import action_bus
import actions
from providers import knowledge_api


def test_identity_resolution_prefers_exact_title_author_and_year():
    results = [
        knowledge_api._row("Index", "A commentary on Computing Machinery and Intelligence",
                           "https://example.com/commentary", "Other Author; 2006", "2006", kind="paper"),
        knowledge_api._row("Crossref", "Computing Machinery and Intelligence (1950)",
                           "https://doi.org/example", "Alan Turing; 1950", "1950", kind="paper"),
    ]
    with mock.patch.object(knowledge_api, "search", return_value={
            "results": results, "errors": {}}):
        resolved = knowledge_api.resolve_paper(
            "Computing Machinery and Intelligence", "Alan Turing", "1950")
    assert resolved["match"]["url"] == "https://doi.org/example"
    assert resolved["match"]["identity_score"] >= 0.68


def test_longer_derivative_title_cannot_beat_exact_title():
    derivative = knowledge_api._row(
        "Index", "Computing Machinery, Intelligence and Undecidability",
        "https://example.com/derivative", "Alan Turing; 1950", "1950", kind="paper")
    exact = knowledge_api._row(
        "Crossref", "Computing Machinery and Intelligence (1950)",
        "https://doi.org/exact", "A. M. Turing", "1950", kind="paper")
    with mock.patch.object(knowledge_api, "search", return_value={
            "results": [derivative, exact], "errors": {}}):
        resolved = knowledge_api.resolve_paper(
            "Computing Machinery and Intelligence", "Alan Turing", "1950")
    assert resolved["match"]["url"] == "https://doi.org/exact"
    assert resolved["match"]["identity_score"] == 1.0


def test_weak_paper_match_is_refused():
    results = [knowledge_api._row(
        "Index", "Unrelated reinforcement learning survey",
        "https://example.com/wrong", "Someone; 2025", "2025", kind="paper")]
    with mock.patch.object(knowledge_api, "search", return_value={
            "results": results, "errors": {}}):
        try:
            knowledge_api.resolve_paper("Computing Machinery and Intelligence")
        except knowledge_api.KnowledgeError:
            pass
        else:
            raise AssertionError("weak identity match was accepted")


def test_open_paper_uses_resolver_url_once_and_returns_identity_receipt():
    events = queue.Queue()
    ctx = action_bus.ActionContext(
        "OPEN_PAPER",
        {"title": "Computing Machinery and Intelligence", "author": "Alan Turing", "year": "1950"},
        events, ".", origin="user",
    )
    match = {"title": "Computing Machinery and Intelligence (1950)",
             "source": "Crossref", "identity_score": .94,
             "url": "https://doi.org/example"}
    with mock.patch.object(knowledge_api, "resolve_paper",
                           return_value={"match": match, "alternatives": [], "errors": {}}), \
         mock.patch("browser.goto", return_value='[Browser Receipt] {"ok":true}') as goto:
        action_bus.BUS.dispatch(ctx)
    goto.assert_called_once_with("https://doi.org/example", tab=None, visible=True)
    assert "Identity score: 0.94" in ctx.receipt
    assert "Do not replace" in ctx.receipt
