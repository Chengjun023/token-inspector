import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from meter import Meter, UsageReader, normalize_usage, price, aggregate_savings, RATES, CREDITS, CHUNK, MAX_LINE


def record(rid="one", tid="root", **usage):
    values = dict(input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0,
                  output_tokens=100, reasoning_output_tokens=80, total_tokens=1100)
    values.update(usage)
    return {"type": "token_usage_record", "payload": {"thread_id": tid, "response_id": rid,
                                                      "usage": values}}


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.reader = UsageReader("root")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, events):
        self.path.write_text("".join(json.dumps(e)+"\n" for e in events))

    def context(self, model="gpt-6-sol"):
        return {"type": "turn_context", "payload": {"model": model, "text": "SECRET"}}

    def test_cache_and_reasoning_are_subsets(self):
        u = normalize_usage(record()["payload"]["usage"])
        usd, credits = price(u, "gpt-6-sol")
        self.assertAlmostEqual(usd, .00156)
        self.assertAlmostEqual(credits, .039)
        self.assertEqual(u["total_tokens"], 1100)

    def test_request_dedup_and_parent_history_exclusion(self):
        self.write([self.context(), record(), record(), record("parent", "other"),
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 5000}}}}])
        self.reader.read(self.path)
        first = self.reader.summary()
        self.reader.read(self.path)
        self.assertEqual(first, self.reader.summary())
        self.assertEqual(first["requests"], 1)
        self.assertEqual(first["totalTokens"], 1100)
        self.assertNotIn("SECRET", json.dumps(first))
        self.assertAlmostEqual(first["savingUSD"], .0078-.00156)

    def test_model_changes_priced_per_context(self):
        self.write([self.context(), record(), self.context("gpt-6-luna"), record("two")])
        self.reader.read(self.path)
        self.assertAlmostEqual(self.reader.summary()["costUSD"], .00156+.000078)
        self.assertEqual(self.reader.summary()["totalTokens"], 2200)

    def test_unknown_model_does_not_create_fake_zero_price(self):
        self.write([record()])
        self.reader.read(self.path)
        self.assertIsNone(self.reader.summary()["costUSD"])
        self.assertEqual(self.reader.summary()["pricedTokens"], 0)
        self.assertEqual(self.reader.summary()["totalTokens"], 1100)
        self.assertIsNone(self.reader.summary()["baselineCredits"])
        self.assertIsNone(self.reader.summary()["savingCredits"])
        self.assertTrue(self.reader.summary()["partial"])

    def savings(self, model="gpt-6-sol", events=None):
        self.reader = UsageReader("root")
        self.write(events if events is not None else [self.context(model), record()])
        self.reader.read(self.path)
        return aggregate_savings([{"id": "root", "usage": self.reader.summary()}])["root"]

    def test_automatic_reference_savings_for_cheaper_model(self):
        savings = self.savings()
        self.assertAlmostEqual(self.reader.summary()["baselineCredits"], .195)
        self.assertAlmostEqual(self.reader.summary()["savingCredits"], .156)
        self.assertAlmostEqual(savings["percent"], 80)
        self.assertEqual(savings["equivalentTokens"], 880)
        self.assertAlmostEqual(savings["savedCredits"], .156)
        self.assertAlmostEqual(savings["savedUSD"], .00624)
        self.assertFalse(savings["partial"])
        self.assertEqual(savings["missingTaskCount"], 0)

    def test_all_astra_reference_savings_is_measured_zero(self):
        savings = self.savings("gpt-6-astra")
        for field in ("percent", "equivalentTokens", "savedCredits", "savedUSD"):
            self.assertEqual(savings[field], 0)
        self.assertFalse(savings["partial"])

    def test_61_sol_uses_new_cache_rate(self):
        usage = normalize_usage(record()["payload"]["usage"])
        self.assertAlmostEqual(price(usage, "gpt-6.1-sol")[0], .00148)
        self.assertAlmostEqual(price(usage, "gpt-6.1-sol")[1], .037)
        savings = self.savings("gpt-6.1-sol")
        self.assertAlmostEqual(savings["savedCredits"], .158)
        self.assertEqual(savings["equivalentTokens"], math.floor(1100 * .158 / .195))
        self.assertEqual(self.reader.summary()["priceDate"], "2026-09-30")

    def test_unknown_fragments_excluded_and_coverage_partial(self):
        savings = self.savings(events=[self.context(), record(), self.context("unknown"), record("two")])
        self.assertEqual((savings["pricedTokens"], savings["totalTokens"]), (1100, 2200))
        self.assertAlmostEqual(savings["percent"], 80)
        self.assertEqual(savings["missingTaskCount"], 0)
        self.assertTrue(savings["partial"])
        unknown = self.savings("unknown")
        for field in ("percent", "equivalentTokens", "savedCredits", "savedUSD"):
            self.assertIsNone(unknown[field])
        self.assertEqual(unknown["missingTaskCount"], 1)

    def test_no_positive_baseline_does_not_fabricate_zero_savings(self):
        savings = self.savings(events=[])
        self.assertIsNone(savings["percent"])
        self.assertIsNone(savings["equivalentTokens"])
        self.assertTrue(savings["partial"])
        self.assertEqual(savings["missingTaskCount"], 1)

    def test_valid_zero_usage_has_no_positive_reference_denominator(self):
        savings = self.savings(events=[self.context(), record(input_tokens=0, cached_input_tokens=0,
                                                             output_tokens=0, reasoning_output_tokens=0)])
        for field in ("percent", "equivalentTokens", "savedCredits", "savedUSD"):
            self.assertIsNone(savings[field])
        self.assertEqual(savings["missingTaskCount"], 0)
        self.assertFalse(savings["partial"])

    def test_reference_cost_increase_is_not_clamped(self):
        with patch.dict(RATES, {"expensive": (20, 2, 25, 100)}), patch.dict(CREDITS, {"expensive": (500, 50, 2500)}):
            savings = self.savings("expensive")
        self.assertAlmostEqual(savings["percent"], -100)
        self.assertEqual(savings["equivalentTokens"], -1100)
        self.assertAlmostEqual(savings["savedCredits"], -.195)
        self.assertAlmostEqual(savings["savedUSD"], -.0078)

    def test_pending_skipped_capped_and_unavailable_are_partial(self):
        self.savings()
        for field, value in (("pending", True), ("skipped", 1), ("capped", True), ("available", False), ("partial", b"incomplete")):
            with self.subTest(field=field):
                original = getattr(self.reader, field)
                setattr(self.reader, field, value)
                self.assertTrue(self.reader.summary()["partial"])
                savings = aggregate_savings([{"id":"root", "usage":self.reader.summary()}])["root"]
                self.assertTrue(savings["partial"])
                setattr(self.reader, field, original)

    def test_descendants_cycles_duplicates_and_depth_limit(self):
        self.savings()
        usage = self.reader.summary()
        agents = [{"id":"root", "parent":"grandchild", "usage":usage},
                  {"id":"child", "parent":"root", "usage":usage},
                  {"id":"grandchild", "parent":"child", "usage":usage},
                  {"id":"child", "parent":"root", "usage":usage}]
        result = aggregate_savings(agents)
        for savings in result.values():
            self.assertEqual(savings["taskCount"], 3)
            self.assertEqual(savings["pricedTokens"], 3300)
            self.assertTrue(savings["includesChildren"])
            self.assertAlmostEqual(savings["savedCredits"], .156 * 3)
        chain = [{"id":str(n), "parent":str(n-1), "usage":usage} for n in range(35)]
        savings = aggregate_savings(chain)["0"]
        self.assertEqual(savings["taskCount"], 33)
        self.assertEqual(savings["pricedTokens"], 33 * 1100)
        self.assertTrue(savings["partial"])

    def test_cache_write_api_surcharge_not_codex_surcharge(self):
        u = normalize_usage(record(cache_write_input_tokens=100)["payload"]["usage"])
        self.assertAlmostEqual(price(u, "gpt-6-sol")[0], .00161)
        self.assertAlmostEqual(price(u, "gpt-6-sol")[1], .039)

    def test_bad_counts_rejected(self):
        for key, value in [("input_tokens", -1), ("input_tokens", True), ("input_tokens", float("nan")),
                           ("cached_input_tokens", 1001), ("reasoning_output_tokens", 101)]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(normalize_usage(record(**{key:value})["payload"]["usage"]))

    def test_incremental_chunks_and_incomplete_line(self):
        self.write([self.context()])
        raw = json.dumps(record())
        with self.path.open("a") as f: f.write(raw[:70])
        self.reader.read(self.path, 100)
        self.reader.read(self.path)
        self.assertEqual(self.reader.summary()["requests"], 0)
        with self.path.open("a") as f: f.write(raw[70:]+"\n")
        self.reader.read(self.path)
        self.assertEqual(self.reader.summary()["requests"], 1)

    def test_rotation_truncation_and_duplicate_ids(self):
        self.write([self.context(), record()]); self.reader.read(self.path)
        new = Path(self.tmp.name) / "next.jsonl"
        new.write_text(json.dumps(self.context())+"\n"+json.dumps(record())+"\n"+json.dumps(record("two"))+"\n")
        self.reader.read(new)
        self.assertEqual(self.reader.summary()["requests"], 2)
        new.write_text(json.dumps(record("three"))+"\n")
        self.reader.read(new)
        self.assertEqual(self.reader.summary()["requests"], 3)
        self.assertEqual(self.reader.summary()["pricedTokens"], 2200)

    def test_large_content_line_is_bounded_and_never_retained(self):
        self.path.write_bytes(b'"'+b'x'*(MAX_LINE*3)+b'"\n'+
                              json.dumps(self.context()).encode()+b'\n'+json.dumps(record()).encode()+b'\n')
        while True:
            n = self.reader.read(self.path)
            self.assertLessEqual(n, CHUNK)
            self.assertLessEqual(len(self.reader.partial), MAX_LINE)
            if not n: break
        self.assertEqual(self.reader.summary()["requests"], 1)

    def test_meter_never_follows_metadata_outside_codex_home(self):
        meter = Meter(Path(self.tmp.name)/"codex")
        self.write([self.context(),record()])
        result = meter.refresh([{"id":"root", "rollout_path":str(self.path)}])
        self.assertEqual(result["root"]["requests"], 0)

    def test_large_recent_rollouts_do_not_starve_rotating_tasks(self):
        meter = Meter(Path(self.tmp.name))
        rows = []
        for n in range(16):
            path = Path(self.tmp.name) / (str(n)+".jsonl")
            path.write_bytes(b"x" * (CHUNK * 2))
            rows.append({"id":str(n),"rollout_path":str(path)})
        meter.refresh(rows)
        self.assertTrue(all(r.offset > 0 for r in meter.readers.values()))
        self.assertLessEqual(sum(r.offset for r in meter.readers.values()), 2*1024*1024)


if __name__ == "__main__":
    unittest.main()
