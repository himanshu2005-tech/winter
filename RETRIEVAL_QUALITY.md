# Retrieval quality

VANTA uses the existing Neo4j graph and existing web providers. These changes
do not introduce another LLM, database, subscription, or embedding model.

## Graph facts

- Fetch a larger bounded candidate pool before choosing the context.
- Rerank by distinctive query terms, predicate/value matches, evidence authority,
  and confidence. Repeated connector syncs do not increase this ranking.
- Collapse duplicate claims in context while retaining graph records.
- Retrieve typed resource locators before a result limit can hide URLs; rank
  those locators against the actual question.
- Rank relationship bridges before expanding the bounded next-hop frontier.
- Apply sensitivity, validity, and inference filters; empty sensitivity lists
  remain empty. Restricted facts stay out of proactive context.
- Preserve fact IDs, provenance, confidence, and review status in model context.
- Semantic retrieval retains its existing vector/full-text fusion. Raw bootstrap
  answers and inactive profile versions are excluded from ordinary retrieval.
- Share four retrieval workers with bounded admission and one turn deadline.
  An unavailable lookup is reported as incomplete, rather than proof that a fact
  does not exist. In-flight provider/driver work may finish after that deadline.

## Web evidence

`SEARCH_WEB` accepts `provider="auto|all|google|tavily"`, `depth`, `limit`, and
optional comma-separated `include_domains` / `exclude_domains`.

- Auto mode reserves time for failover; an empty primary response can also trigger
  fallback. `all` uses the configured providers for broader coverage.
- Fuse provider ranks, then rank query coverage and distinctive term matches.
  Provider-specific relevance scores are not treated as comparable trust scores.
- Canonicalize tracking URLs without lowercasing case-sensitive paths or values.
  Merge duplicate sources while keeping provider provenance and richer excerpts.
- Apply domain restrictions to every provider. Invalid URLs are excluded.
- Include citation IDs, exact URLs, extraction type, publication metadata when
  available, and retrieval timestamps. Allocate context across sources and prefer
  relevant passages over navigation text at the beginning of a page.
- Date mentions and provider summaries never constitute independent verification.
  Check source pages for consequential deadlines, prices, and availability.
- Cache deep copies, isolate query options, and avoid caching empty failures.
  Search retries have a time budget; authentication failures are not retried.

## Verification

Run the focused regression suite:

```powershell
$env:PYTHONPATH='D:\winter\.venv\Lib\site-packages;D:\winter'
python -m pytest -q tests/test_retrieval_quality.py tests/test_advanced_search.py tests/test_memory_grounding.py tests/test_memory_retrieval_fallback.py tests/test_evidence_quality.py tests/test_result_presenter.py
```

Read-only live smoke checks (the web call consumes configured provider quota):

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_retrieval --graph "my portfolio website" --web "Python 3.11 asyncio TaskGroup official documentation"
```

The smoke script prints selection counts and latency, not personal fact content
or credentials. Ranking tests use synthetic noisy candidates. These checks do
not establish universal accuracy, full browser verification, or production
latency guarantees; returned evidence still needs task-specific interpretation.
