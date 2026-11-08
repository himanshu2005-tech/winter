from unittest.mock import Mock, patch

from providers import pricing_api
import watches


def payload():
    return {
        "shopping_results": [
            {"position": 1, "title": "Apple MacBook Air M1 8GB 256GB",
             "extracted_price": 30000, "source": "Used Store",
             "product_link": "https://example.com/old", "product_id": "m1-256"},
            {"position": 2, "title": "MacBook Air M4 16GB 512GB",
             "extracted_price": 99900, "source": "Store B",
             "product_link": "https://example.com/b", "product_id": "m4-512"},
            {"position": 1, "title": "MacBook Air M4 16GB 512GB",
             "price": "₹1,04,900", "source": "Store A",
             "product_link": "https://example.com/a", "product_id": "m4-512"},
        ]
    }


def test_structured_product_prices_are_normalized_and_cached():
    pricing_api._cache.clear()
    reply = Mock(status_code=200)
    reply.raise_for_status.return_value = None
    reply.json.return_value = payload()
    with patch.object(pricing_api, "_key", return_value="test"), \
         patch.object(pricing_api.requests, "get", return_value=reply) as get:
        first = pricing_api.search("MacBook Air M4 16GB 512GB")
        second = pricing_api.search("MacBook Air M4 16GB 512GB")
    assert first["lowest_price"] == 99900
    assert first["median_price"] == 102400
    assert first["offers"][0]["merchant"] == "Store B"
    assert first["rejected_loose_matches"] == 1
    assert second["cached"] is True
    assert get.call_count == 1


def test_product_id_uses_exact_product_offer_endpoint():
    pricing_api._cache.clear()
    reply = Mock(status_code=200)
    reply.raise_for_status.return_value = None
    reply.json.return_value = {
        "product_results": {
            "title": "MacBook Air M4 16GB 512GB",
            "stores": [
                {"position": 1, "name": "Exact Store", "price": "₹98,500",
                 "extracted_price": 98500, "link": "https://example.com/exact"}
            ],
        }
    }
    with patch.object(pricing_api, "_key", return_value="test"), \
         patch.object(pricing_api.requests, "get", return_value=reply) as get:
        result = pricing_api.search("MacBook", product_id="m4-512")
    assert result["lowest_price"] == 98500
    assert result["offers"][0]["merchant"] == "Exact Store"
    assert result["source"] == "SerpApi Google Product"
    assert get.call_args.kwargs["params"]["engine"] == "google_product"
    assert get.call_args.kwargs["params"]["offer_view"] == "true"


class Store:
    def __init__(self):
        self.params = None

    def query(self, _cypher, **params):
        self.params = params
        return [{"id": params.get("id"), "created_at": params.get("now")}]


def test_product_watch_is_structured_durable_and_quota_safe(monkeypatch):
    monkeypatch.setattr(watches, "PRODUCT_PRICE_MIN_INTERVAL_SECONDS", 12 * 3600)
    store = Store()
    result = watches.create_product_price(
        "MacBook Air M4 16GB 512GB", "1 lakh", product_id="m4-512",
        interval="5m", store_obj=store)
    assert result.startswith("[Success]")
    assert store.params["kind"] == "product_price"
    assert store.params["interval"] == 12 * 3600
    assert '"product_id":"m4-512"' in store.params["source"]


def test_product_watch_refuses_ambiguous_query_without_product_id():
    result = watches.create_product_price(
        "MacBook Air", "1 lakh", store_obj=Store())
    assert result.startswith("[Error]")
    assert "product_id" in result


def test_product_watch_observation_uses_pricing_api():
    watch = {
        "kind": "product_price",
        "source": ('{"location":"India","product_id":"m4-512",'
                   '"query":"MacBook Air M4"}'),
    }
    result = {
        "lowest_price": 98500, "currency": "INR",
        "offers": [{"price": 98500, "currency": "INR", "title": "MacBook",
                    "merchant": "Example", "product_id": "m4-512",
                    "link": "https://example.com"}],
    }
    with patch.object(pricing_api, "search", return_value=result):
        observed = watches.observe(watch)
    assert observed["value"] == 98500
    assert observed["method"] == "SerpApi Google Shopping"


def test_price_capabilities_are_registered_and_default_denied_proactively():
    import action_schema
    import autonomy
    import capabilities

    assert capabilities.get("PRICE_SEARCH") is not None
    assert capabilities.get("PRICE_WATCH_CREATE") is not None
    assert action_schema.validate(
        "PRICE_SEARCH", {"type": "PRICE_SEARCH", "query": "laptop"}) is None
    assert "product_id" in action_schema.validate(
        "PRICE_WATCH_CREATE",
        {"type": "PRICE_WATCH_CREATE", "query": "laptop", "target": "1000"})
    assert not autonomy.action_allowed("PRICE_SEARCH", "proactive")[0]
    assert not autonomy.action_allowed("PRICE_WATCH_CREATE", "proactive")[0]
