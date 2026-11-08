"""Read-only smoke checks; --web uses the configured search-provider quota.

No raw personal facts, query contents, or credentials are printed.
Example: python -m scripts.evaluate_retrieval --graph "my portfolio website"
"""
import argparse
import json
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', action='append', default=[], metavar='QUERY')
    parser.add_argument('--web', action='append', default=[], metavar='QUERY')
    args = parser.parse_args()
    if not args.graph and not args.web:
        parser.error('Provide at least one --graph or --web query.')
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    records = []
    for number, query in enumerate(args.graph, 1):
        from cognitive_pipeline import CognitiveRoute, RetrievalRoute
        from memory.orchestrator import get_context_for_turn
        from resource_pool import ResourceRequest
        route = CognitiveRoute(
            'keep', 'assistant', 'respond', 'question', 'direct', False, 1.0,
            RetrievalRoute(graph=True),
            resource_requests=(ResourceRequest('graph_memory', query, True),))
        start = time.monotonic()
        bundle = get_context_for_turn(query, cognitive_route=route, timeout_sec=5)
        records.append(dict(kind='graph', case=number,
                            elapsed_ms=round((time.monotonic()-start)*1000),
                            fact_count=bundle.debug_record['graph_edges'],
                            url_present='https://' in bundle.graph_text or 'http://' in bundle.graph_text,
                            degraded=bundle.debug_record['degradation']))
    for number, query in enumerate(args.web, 1):
        from providers.research_api import advanced_search
        start = time.monotonic()
        try:
            result = advanced_search(query, max_results=5)
            records.append(dict(kind='web', case=number,
                                elapsed_ms=round((time.monotonic()-start)*1000),
                                source_count=len(result['results']),
                                providers=result['providers'], cache_hit=result['cache_hit'],
                                verified=result['evidence']['verified_for_requested_date']))
        except Exception as exc:
            records.append(dict(kind='web', case=number, error=type(exc).__name__))
    print(json.dumps(records, indent=2))


if __name__ == '__main__':
    main()
