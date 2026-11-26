# -*- coding: utf-8 -*-
"""
Tests for the free-API directory.

Why a directory and not 913 more actions: the key-free subset of the public-apis
list is 913 entries, which written into the prompt would be roughly 274,000
characters on every turn against the 22,500 assistant mode already spends. It
would also be the wrong move if it were free, because prompt bloat is the
measured failure mode here -- a model that scored 6/6 on a clean prompt failed in
the real system and invented BROWSER_SEARCH twice while holding the browser
catalogue.

So the list is data, one action searches it, and CREATE_ACTION turns the entry
that fits into a real tool. The prompt grows by a line; the toolset grows only
where something needed it.

Parsing and search run offline against a fixture. The live refresh is opt-in.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Two categories, including one entry needing a key. Shaped like the real README.
FIXTURE = """
### Weather

| API | Description | Auth | HTTPS | CORS |
|---|---|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | Free weather forecast | No | Yes | Yes |
| [AccuWeather](https://accuweather.com/) | Weather forecast | `apiKey` | Yes | Unknown |

### Currency Exchange

| API | Description | Auth | HTTPS | CORS |
|---|---|---|---|---|
| [Currency-api](https://github.com/fawazahmed0/currency-api) | Exchange rates, no rate limits | No | Yes | Yes |
"""


class ParseTests(unittest.TestCase):

    def setUp(self):
        self.entries = api_index._parse(FIXTURE)

    def test_keyed_and_key_free_entries_are_kept_with_auth_metadata(self):
        names = [e["name"] for e in self.entries]
        self.assertIn("Open-Meteo", names)
        self.assertIn("Currency-api", names)
        self.assertIn("AccuWeather", names)
        by_name = {e["name"]: e for e in self.entries}
        self.assertEqual(by_name["AccuWeather"]["auth"], "apiKey")

    def test_each_entry_knows_its_category(self):
        # The category does most of the work in a search: "weather" matches a
        # heading long before it matches any one description.
        by_name = {e["name"]: e for e in self.entries}
        self.assertEqual(by_name["Open-Meteo"]["category"], "Weather")
        self.assertEqual(by_name["Currency-api"]["category"],
                         "Currency Exchange")

    def test_the_url_and_description_survive(self):
        entry = self.entries[0]
        self.assertTrue(entry["url"].startswith("https://"))
        self.assertIn("weather", entry["description"].lower())

    def test_https_is_recorded(self):
        self.assertTrue(all(e["https"] for e in self.entries))

    def test_rubbish_parses_to_nothing_rather_than_raising(self):
        self.assertEqual(api_index._parse("not a markdown table at all"), [])
        self.assertEqual(api_index._parse(""), [])


class SearchTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vanta_api_index_")
        os.makedirs(os.path.join(self.dir, "vault", "system"), exist_ok=True)
        entries = api_index._parse(FIXTURE)
        with open(api_index.index_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump({"count": len(entries), "entries": entries}, fh)
        api_index._cache["entries"] = None
        api_index._cache["path"] = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        api_index._cache["entries"] = None
        api_index._cache["path"] = None

    def test_a_category_word_finds_its_entries(self):
        hits = api_index.search(self.dir, "weather forecast")
        self.assertTrue(hits)
        self.assertEqual({hit["name"] for hit in hits},
                         {"Open-Meteo", "AccuWeather"})

    def test_a_description_word_finds_its_entry(self):
        hits = api_index.search(self.dir, "exchange rates")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["name"], "Currency-api")

    def test_nothing_relevant_returns_nothing(self):
        self.assertEqual(api_index.search(self.dir, "quantum tunnelling"), [])

    def test_short_words_are_ignored(self):
        # Otherwise "a" and "of" match everything and rank nothing.
        self.assertEqual(api_index.search(self.dir, "a of to"), [])

    def test_the_limit_is_respected(self):
        self.assertLessEqual(len(api_index.search(self.dir, "free", limit=1)), 1)

    def test_auth_filter_can_restrict_to_key_free(self):
        hits = api_index.search(self.dir, "weather", auth="none")
        self.assertEqual([hit["name"] for hit in hits], ["Open-Meteo"])

    def test_auth_filter_can_find_credentialed_services(self):
        hits = api_index.search(self.dir, "weather", auth="required")
        self.assertEqual([hit["name"] for hit in hits], ["AccuWeather"])

    def test_the_index_is_cached_after_the_first_read(self):
        api_index.search(self.dir, "weather")
        self.assertIsNotNone(api_index._cache["entries"])


class DescribeTests(unittest.TestCase):
    """What the model reads back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vanta_api_desc_")
        os.makedirs(os.path.join(self.dir, "vault", "system"), exist_ok=True)
        api_index._cache["entries"] = None
        api_index._cache["path"] = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        api_index._cache["entries"] = None
        api_index._cache["path"] = None

    def _write_fixture(self):
        entries = api_index._parse(FIXTURE)
        with open(api_index.index_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump({"count": len(entries), "entries": entries}, fh)

    def test_an_unbuilt_index_says_how_to_build_it(self):
        text = api_index.describe(self.dir, "weather")
        self.assertIn("not been built", text)
        self.assertIn("refresh", text)

    def test_a_hit_carries_the_endpoint(self):
        self._write_fixture()
        text = api_index.describe(self.dir, "weather")
        self.assertIn("Open-Meteo", text)
        self.assertIn("https://open-meteo.com/", text)

    def test_it_points_at_create_action(self):
        # Finding the endpoint is half the job; the tool has to persist.
        self._write_fixture()
        text = api_index.describe(self.dir, "weather")
        self.assertIn("CREATE_ACTION", text)
        self.assertIn("RUN_PYTHON", text)

    def test_a_miss_says_how_many_were_searched(self):
        self._write_fixture()
        text = api_index.describe(self.dir, "quantum tunnelling")
        self.assertIn("No API", text)
        self.assertIn("3", text)


class WiringTests(unittest.TestCase):

    def test_it_is_registered_or_it_is_refused(self):
        import autonomy
        self.assertIn("FIND_API", autonomy.KNOWN_ACTIONS)

    def test_the_schema_knows_its_arguments(self):
        import action_schema
        spec = action_schema.ACTION_SPECS["FIND_API"]
        self.assertEqual(spec["requires"], ["need"])
        self.assertIn("refresh", spec["optional"])
        self.assertIn("region", spec["optional"])

    def test_the_model_is_told_it_exists(self):
        import agent
        self.assertIn("FIND_API", agent.ACTION_CATALOGS["SYSTEM"])

    def test_the_catalogue_says_not_to_give_up_first(self):
        # The failure this guards against is concluding a capability is absent
        # when only the tool is.
        import agent
        entry = agent.ACTION_CATALOGS["SYSTEM"]
        self.assertIn("Do not conclude something is impossible", entry)


@unittest.skipUnless(os.getenv("WINTER_NETWORK_TESTS"),
                     "set WINTER_NETWORK_TESTS=1 to fetch the real list")
class LiveTests(unittest.TestCase):

    def test_the_real_list_indexes(self):
        directory = tempfile.mkdtemp(prefix="vanta_api_live_")
        os.makedirs(os.path.join(directory, "vault", "system"), exist_ok=True)
        try:
            receipt = api_index.refresh(directory)
            if receipt.startswith("[Error]"):
                self.skipTest(receipt)
            self.assertIn("[Success]", receipt)
            entries = api_index.load(directory)
            self.assertGreater(len(entries), 1000,
                               "the full catalog should be over a thousand entries")
            self.assertTrue(all(e["name"] for e in entries))
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)
            api_index._cache["entries"] = None
            api_index._cache["path"] = None


if __name__ == "__main__":
    unittest.main()
