import copy
import datetime as dt
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_registry as registry

NOW = "2026-10-01T00:00:00Z"
FEED = {
    "other-provider": {"models": {
        "new-model": {"id": "new-model", "name": "New model", "reasoning": True,
                      "tool_call": True, "cost": {"input": .5, "output": 2, "cache_read": .1},
                      "limit": {"context": 128000}, "npm": "do-not-execute-this"}}},
    "openai": {"models": {
        "gpt-future": {"id": "gpt-future", "name": "Future model", "reasoning": True,
                       "cost": {"input": 1, "output": 4}, "limit": {"context": 256000}}}},
}
USAGE = {"input_tokens": 1000, "cached_input_tokens": 200,
         "cache_write_input_tokens": 100, "output_tokens": 50}


class Registry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.feed_path = self.home / "feed.json"
        self.feed_path.write_text(json.dumps(FEED), encoding="utf-8")

    def imported(self, now=NOW):
        return registry.import_feed(self.feed_path, self.home, now=now)

    def test_bundled_has_verified_sources_prices_and_efforts(self):
        snapshot = registry.bundled_snapshot()
        self.assertEqual(len(snapshot["models"]), 7)
        model = registry.get_model(snapshot, "gpt-6.1-sol")
        self.assertEqual(model["pricing"]["as_of"], "2026-09-30")
        self.assertEqual(model["pricing"]["usd_per_million"]["cached_input"], .1)
        self.assertEqual(model["pricing"]["credits_per_million"]["cached_input"], 2.5)
        self.assertIn("ultra", model["capabilities"]["efforts"])
        self.assertFalse(model["routing"]["verified"])
        self.assertIsNone(model["routing"]["latency_ms"])
        self.assertTrue(registry.get_model(snapshot, "gpt-6-astra")["routing"]["safety_equivalent_verified"])

    def test_default_load_never_downloads_or_creates_files(self):
        with patch.object(registry, "_download", side_effect=AssertionError("offline")):
            snapshot = registry.load_snapshot(self.home, now=NOW)
        self.assertEqual(snapshot["cache_status"], "bundled")
        self.assertFalse((self.home / "model-registry").exists())

    def test_import_creates_lastgood_and_immutable_history(self):
        snapshot = self.imported()
        self.assertEqual(snapshot["cache_status"], "cached")
        lastgood = self.home / "model-registry/lastgood.json"
        history = self.home / "model-registry/snapshots" / (snapshot["hash"].split(":")[1] + ".json")
        self.assertEqual(registry.read_snapshot(lastgood)["hash"], snapshot["hash"])
        self.assertEqual(lastgood.read_bytes(), history.read_bytes())

    def test_unknown_model_is_candidate_never_native(self):
        snapshot = self.imported()
        model = registry.get_model(snapshot, "gpt-future")
        self.assertEqual(model["status"], "candidate")
        self.assertFalse(model["capabilities"]["verified"])
        self.assertEqual(model["capabilities"]["efforts"], [])
        self.assertEqual(model["endpoints"][0]["kind"], "api")
        self.assertEqual(registry.eligible_models(snapshot, {"gpt-future": ["high"]}), [])

    def test_live_endpoints_and_efforts_are_required(self):
        snapshot = registry.bundled_snapshot()
        result = registry.eligible_models(snapshot, {"gpt-6.1-sol": ["high", "none"]})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["eligible_endpoints"][0]["efforts"], ["high"])
        self.assertEqual(registry.eligible_models(snapshot, ["gpt-6.1-sol"]), [])
        self.assertEqual(registry.eligible_models(snapshot, {"gpt-6-luna": ["ultra"]}), [])
        self.assertEqual(registry.eligible_models(snapshot, {"gpt-6.1-sol": ["high"]}, provider="other-provider"), [])

    def test_explicit_admission_still_needs_actual_availability(self):
        self.imported()
        endpoint = {"provider": "codex", "id": "future-host-slug", "kind": "native", "verified": False}
        snapshot = registry.admit_model("gpt-future", home=self.home, efforts=["high"], endpoints=[endpoint], evidence="本机工具目录已观察到此slug")
        self.assertEqual(registry.get_model(snapshot, "gpt-future")["status"], "admitted")
        self.assertFalse(registry.get_model(snapshot, "gpt-future")["capabilities"]["verified"])
        self.assertEqual(registry.eligible_models(snapshot, {"gpt-future": ["high"]}), [])
        self.assertEqual(len(registry.eligible_models(snapshot, {"future-host-slug": ["high"]})), 1)
        self.assertEqual(registry.eligible_models(snapshot, {"future-host-slug": ["medium"]}), [])

    def test_future_model_local_admission_host_and_live_selection_loop(self):
        self.imported()
        endpoint = {"provider": "codex", "id": "future-native", "kind": "native", "verified": False}
        snapshot = registry.admit_model(
            "gpt-future", home=self.home, efforts=["medium", "high"], endpoints=[endpoint],
            evidence="本地工具目录、工具调用与验收检查均通过；合成测试记录不表示实测质量",
            capabilities={"verified": True, "tools": True},
            routing={"tier": "standard", "verified": True, "strengths": {"code": .8}})
        model = registry.get_model(snapshot, "gpt-future")
        self.assertTrue(model["capabilities"]["verified"])
        self.assertEqual(model["routing"]["tier"], "standard")
        self.assertFalse(model["routing"]["safety_equivalent_verified"])
        self.assertEqual(registry.eligible_models(snapshot, {}), [])
        registry.admit_host("future-native", ["high"], "本机spawn工具目录已核对", home=self.home)
        extensions = registry.host_models(self.home)
        actual = {"future-native": ["medium", "high"]}
        usable = {key: [effort for effort in actual.get(key, []) if effort in declared]
                  for key, declared in extensions.items()}
        chosen = registry.eligible_models(snapshot, usable)
        self.assertEqual(chosen[0]["id"], "gpt-future")
        self.assertEqual(chosen[0]["eligible_endpoints"][0]["efforts"], ["high"])
        self.assertEqual(registry.eligible_models(snapshot, {"future-native": []}), [])
        archive = self.home / "model-registry/snapshots" / (snapshot["hash"].split(":")[1] + ".json")
        self.assertTrue(archive.exists())

    def test_explicit_price_adoption_changes_current_preserves_frozen_reference(self):
        feed = {"openai": {"models": {"gpt-6.1-sol": {"cost": {"input": 1, "output": 4, "cache_read": .05, "cache_write": 1.25}}}}}
        self.feed_path.write_text(json.dumps(feed))
        before = self.imported()
        original_cost = registry.price_usage(before, "gpt-6.1-sol", USAGE)
        after = registry.adopt_pricing("gpt-6.1-sol", home=self.home,
                                       evidence="已核对当前公开费率及Standard短上下文计费范围", verified=True)
        new_cost = registry.price_usage(after, "gpt-6.1-sol", USAGE)
        self.assertNotEqual(after["hash"], before["hash"])
        self.assertLess(new_cost["usd"], original_cost["usd"])
        self.assertIsNone(new_cost["credits"])
        self.assertTrue(new_cost["price_verified"])
        original_path = self.home / "model-registry/snapshots" / (before["hash"].split(":")[1] + ".json")
        historical = registry.price_usage(registry.read_snapshot(original_path), "gpt-6.1-sol", USAGE)
        self.assertEqual(historical, original_cost)
        self.assertEqual(registry.price_usage(registry.load_snapshot(self.home), "gpt-6.1-sol", USAGE), new_cost)

    def test_invalid_local_verified_fields_rejected_before_policy_write(self):
        self.imported()
        with self.assertRaises(registry.RegistryError):
            registry.admit_model("gpt-future", home=self.home, efforts=["high"],
                                 endpoints=[{"provider": "codex", "id": "future", "kind": "native", "verified": False}],
                                 evidence="测试", routing={"verified": "yes", "tier": "frontier"})
        self.assertFalse((self.home / "model-registry/policy.json").exists())

    def test_disable_persists_across_feed_update(self):
        self.imported()
        registry.disable_model("gpt-6-astra", home=self.home)
        snapshot = self.imported("2026-10-02T00:00:00Z")
        self.assertEqual(registry.get_model(snapshot, "gpt-6-astra")["status"], "disabled")
        self.assertEqual(registry.eligible_models(snapshot, {"gpt-6-astra": ["high"]}), [])

    def test_host_expansion_requires_evidence_and_is_separate(self):
        self.assertEqual(registry.host_models(self.home), {})
        with self.assertRaises(registry.RegistryError):
            registry.admit_host("future", ["high"], "", self.home)
        registry.admit_host("future", ["high"], "已核对宿主工具能力", self.home)
        self.assertEqual(registry.host_models(self.home), {"future": ["high"]})
        self.assertEqual(registry.host_models(self.home, "different"), {})

    def test_fresh_cache_skips_download(self):
        self.imported()
        with patch.object(registry, "_download", side_effect=AssertionError("TTL")):
            snapshot = registry.refresh_snapshot(self.home, now="2026-10-01T00:05:00Z")
        self.assertFalse(snapshot["stale"])

    def test_expired_cache_refreshes(self):
        first = self.imported()
        with patch.object(registry, "_download", return_value=FEED) as download:
            second = registry.refresh_snapshot(self.home, now="2026-10-03T00:00:00Z")
        self.assertEqual(download.call_count, 1)
        self.assertEqual(second["cache_status"], "refreshed")
        self.assertNotEqual(second["hash"], first["hash"])
        self.assertFalse(second["stale"])

    def test_first_explicit_refresh_fetches_even_with_recent_bundle(self):
        with patch.object(registry, "_download", return_value=FEED) as download:
            snapshot = registry.load_snapshot(self.home, refresh=True, now=NOW)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(snapshot["cache_status"], "refreshed")

    def test_network_failure_preserves_lastgood_and_cooldown(self):
        first = self.imported()
        path = self.home / "model-registry/lastgood.json"
        original = path.read_bytes()
        failure = urllib.error.HTTPError(registry.FEED_URL, 403, "Forbidden", {}, None)
        with patch.object(registry, "_download", side_effect=failure):
            result = registry.refresh_snapshot(self.home, now="2026-10-03T00:00:00Z")
        self.assertEqual(result["hash"], first["hash"])
        self.assertTrue(result["stale"])
        self.assertIn("HTTP 403", result["error"])
        self.assertEqual(path.read_bytes(), original)
        with patch.object(registry, "_download", side_effect=AssertionError("cooldown")):
            again = registry.refresh_snapshot(self.home, now="2026-10-03T00:01:00Z")
        self.assertEqual(again["cache_status"], "cooldown")
        self.assertTrue(registry.load_snapshot(self.home, now="2026-10-03T00:01:00Z")["stale"])

    def test_force_skips_cooldown_and_recovers(self):
        self.imported()
        with patch.object(registry, "_download", side_effect=OSError("offline")):
            registry.refresh_snapshot(self.home, now="2026-10-03T00:00:00Z")
        with patch.object(registry, "_download", return_value=FEED):
            snapshot = registry.refresh_snapshot(self.home, now="2026-10-03T00:00:10Z", force=True)
        self.assertFalse(snapshot["stale"])
        self.assertIsNone(snapshot["error"])

    def test_bad_feed_preserves_lastgood_bytes(self):
        self.imported()
        original = (self.home / "model-registry/lastgood.json").read_bytes()
        invalid = copy.deepcopy(FEED)
        invalid["other-provider"]["models"]["new-model"]["cost"]["input"] = -1
        invalid["openai"]["models"]["gpt-future"]["cost"]["input"] = -1
        self.feed_path.write_text(json.dumps(invalid))
        with self.assertRaises(registry.RegistryError):
            registry.import_feed(self.feed_path, self.home, now=NOW)
        self.assertEqual((self.home / "model-registry/lastgood.json").read_bytes(), original)

    def test_nonfinite_boolean_negative_and_nested_bad_cost_rejected(self):
        for price in (-1, float("inf"), float("nan"), True, "1"):
            feed = copy.deepcopy(FEED)
            feed["openai"]["models"]["gpt-future"]["cost"]["input"] = price
            with self.subTest(price=price):
                if isinstance(price, float) and not math.isfinite(price):
                    with self.assertRaises(registry.RegistryError):
                        registry.convert_feed(feed, now=NOW)
                else:
                    snapshot = registry.convert_feed(feed, now=NOW)
                    self.assertIsNone(registry.get_model(snapshot, "gpt-future"))
                    self.assertEqual(snapshot["source"]["skipped_models"], 1)
        feed = copy.deepcopy(FEED)
        feed["openai"]["models"]["gpt-future"]["cost"]["over_200k"] = {"input": -1}
        self.assertEqual(registry.convert_feed(feed, now=NOW)["source"]["skipped_models"], 1)

    def test_size_duplicate_json_and_invalid_context_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry._decode(b" " * (registry.MAX_JSON_BYTES + 1))
        with self.assertRaises(registry.RegistryError):
            registry._decode(b'{"openai":{},"openai":{}}')
        with self.assertRaises(registry.RegistryError):
            registry._decode(b'{"price":NaN}')
        feed = copy.deepcopy(FEED)
        feed["openai"]["models"]["gpt-future"]["limit"]["context"] = True
        snapshot = registry.convert_feed(feed, now=NOW)
        self.assertIsNone(registry.get_model(snapshot, "gpt-future"))
        self.assertEqual(snapshot["source"]["skipped_models"], 1)

    def test_real_shape_tiers_bad_name_negative_sentinel_and_unknown_price(self):
        fixture = Path(__file__).resolve().parents[1] / "data" / "models.dev.shape.fixture.json"
        snapshot = registry.convert_feed(json.loads(fixture.read_text()), now=NOW)
        self.assertEqual(snapshot["source"]["input_models"], 4)
        self.assertEqual(snapshot["source"]["skipped_models"], 2)
        valid = registry.get_model(snapshot, "valid-tiered", "synthetic-provider")
        self.assertEqual(valid["pricing"]["usd_per_million"]["input"], 2)
        unknown = registry.get_model(snapshot, "unknown-price", "synthetic-provider")
        self.assertIsNone(unknown["pricing"]["usd_per_million"]["input"])
        self.assertFalse(registry.price_usage(snapshot, "unknown-price", USAGE, "synthetic-provider")["known"])

    def test_normalized_cache_can_exceed_feed_limit_but_remains_bounded(self):
        snapshot = registry.convert_feed(FEED, now=NOW)
        snapshot["source"]["padding"] = "x" * 2000
        snapshot = registry._sealed(snapshot)
        path = self.home / "normalized.json"
        with patch.object(registry, "MAX_JSON_BYTES", 1000):
            registry.freeze_snapshot(snapshot, path)
            self.assertEqual(registry.read_snapshot(path)["hash"], snapshot["hash"])

    def test_hash_detects_tamper(self):
        snapshot = registry.bundled_snapshot()
        snapshot["models"][0]["pricing"]["usd_per_million"]["input"] = 0
        with self.assertRaises(registry.RegistryError):
            registry.validate_snapshot(snapshot)

    def test_stale_metadata_never_changes_content_hash(self):
        snapshot = self.imported()
        stale = registry.load_snapshot(self.home, now="2026-10-03T00:00:00Z")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["hash"], snapshot["hash"])
        self.assertEqual(registry.snapshot_hash(stale), snapshot["hash"])

    def test_freezing_is_immutable_and_prices_are_reproducible(self):
        snapshot = self.imported()
        frozen_path = self.home / "frozen.json"
        frozen_hash = registry.freeze_snapshot(snapshot, frozen_path)
        self.assertEqual(registry.freeze_snapshot(snapshot, frozen_path), frozen_hash)
        original = frozen_path.read_bytes()
        updated = registry.convert_feed(FEED, now="2026-10-03T00:00:00Z")
        with self.assertRaises(registry.RegistryError):
            registry.freeze_snapshot(updated, frozen_path)
        self.assertEqual(original, frozen_path.read_bytes())
        result = registry.price_usage(registry.read_snapshot(frozen_path), "gpt-6.1-sol", USAGE)
        self.assertEqual(result["snapshot_hash"], frozen_hash)
        self.assertAlmostEqual(result["usd"], (700*2 + 200*.1 + 100*2.5 + 50*10)/1e6)
        self.assertAlmostEqual(result["credits"], (800*50 + 200*2.5 + 50*250)/1e6)

    def test_feed_does_not_replace_verified_reference_pricing(self):
        feed = {"openai": {"models": {"gpt-6.1-sol": {"cost": {"input": .001, "output": .001}}}}}
        snapshot = registry.convert_feed(feed, now=NOW)
        model = registry.get_model(snapshot, "gpt-6.1-sol")
        self.assertEqual(model["pricing"]["usd_per_million"]["input"], 2)
        self.assertEqual(model["public_metadata"]["pricing"]["usd_per_million"]["input"], .001)

    def test_unknown_cost_and_missing_cache_write_are_null(self):
        snapshot = self.imported()
        result = registry.price_usage(snapshot, "missing", USAGE)
        self.assertIsNone(result["usd"])
        self.assertFalse(result["known"])
        result = registry.price_usage(snapshot, "new-model", USAGE, provider="other-provider")
        self.assertIsNone(result["usd"])
        self.assertIsNone(result["credits"])
        usage = dict(USAGE, cache_write_input_tokens=0)
        result = registry.price_usage(snapshot, "new-model", usage, provider="other-provider")
        self.assertTrue(result["known"])
        self.assertFalse(result["price_verified"])

    def test_bad_usage_rejected(self):
        for usage in (dict(USAGE, input_tokens=True), dict(USAGE, output_tokens=-1), dict(USAGE, cached_input_tokens=1001)):
            with self.assertRaises(registry.RegistryError):
                registry.price_usage(registry.bundled_snapshot(), "gpt-6.1-sol", usage)

    def test_https_only_without_credentials_or_queries(self):
        for url in ("http://models.dev/api.json", "https://user:password@models.dev/api.json", "https://models.dev/api.json?token=secret", "file:///tmp/feed"):
            with self.subTest(url=url), self.assertRaises(registry.RegistryError):
                registry.convert_feed(FEED, source_url=url, now=NOW)

    def test_corrupt_cache_falls_back_explicitly(self):
        self.imported()
        (self.home / "model-registry/lastgood.json").write_text("{}")
        result = registry.load_snapshot(self.home, now=NOW)
        self.assertEqual(result["cache_status"], "cache_invalid")
        self.assertTrue(result["stale"])
        self.assertEqual(result["hash"], registry.bundled_snapshot()["hash"])

    def test_cli_import_and_freeze_work_with_spaces(self):
        root = Path(__file__).resolve().parents[1]
        for args in (("import", str(self.feed_path)), ("freeze", str(self.home / "frozen file.json"))):
            completed = subprocess.run([sys.executable, str(root / "scripts/model_registry.py"), "--home", str(self.home)] + list(args), capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("hash", json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
