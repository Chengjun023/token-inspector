"""Bounded, local-only usage accounting. No model, tokenizer or network calls."""
import json
from pathlib import Path
import datetime
import copy
import math
import sqlite3
import time
from usage_ledger import UsageLedger

try:
    import model_registry
except ImportError:
    model_registry = None

# USD / 1M tokens: input, cached input, cache writes, output.
# Snapshot of the rendered official Standard / short-context table, 2026-09-30.
# https://developers.openai.com/api/docs/pricing
# https://developers.openai.com/api/docs/models/gpt-6.1-sol
RATES = {
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-6.1-sol": (2, .1, 2.5, 10),
    "gpt-6-sol": (2, .2, 2.5, 10),
    "gpt-6-luna": (.1, .01, .125, .5),
    "gpt-5.6-sol": (4, .4, 5, 20),
    "gpt-5.6-terra": (2, .2, 2.5, 12),
    "gpt-5.6-luna": (.2, .02, .25, 1.2),
}
# Standard credits / 1M: input, cached input, output. No cache-write surcharge.
# https://learn.chatgpt.com/docs/pricing#token-rates
CREDITS = {
    "gpt-6-astra": (250, 25, 1250), "gpt-6-sol": (50, 5, 250),
    "gpt-6.1-sol": (50, 2.5, 250),
    "gpt-6-luna": (2.5, .25, 12.5), "gpt-5.6-sol": (100, 10, 500),
    "gpt-5.6-terra": (50, 5, 300), "gpt-5.6-luna": (5, .5, 30),
}
PRICE_DATE = "2026-09-30"
MAX_LINE = 512 * 1024
CHUNK = 256 * 1024
MAX_IDS = 4096


def normalize_usage(raw):
    if not isinstance(raw, dict) or not {"input_tokens", "output_tokens"}.issubset(raw):
        return None
    values = {}
    for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens"):
        v = raw.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10**12:
            return None
        values[k] = v
    i, c, w, o, r = values.values()
    if c + w > i or r > o:
        return None
    # Reasoning is a subset of output, cached input a subset of input.
    values["total_tokens"] = i + o
    return values


def price(usage, model):
    rates = RATES.get(model)
    if not rates:
        return None
    i, c, w, o = (usage[k] for k in ("input_tokens", "cached_input_tokens",
                                    "cache_write_input_tokens", "output_tokens"))
    usd = ((i-c-w)*rates[0] + c*rates[1] + w*rates[2] + o*rates[3]) / 1e6
    cr = CREDITS[model]
    credits = ((i-c)*cr[0] + c*cr[1] + o*cr[2]) / 1e6
    return usd, credits


class PricingCatalog:
    """Read the router's validated local snapshot, never fetch from the monitor."""
    def __init__(self, home):
        self.home = Path(home) / 'adaptive-router'
        self.snapshot = None
        self.loaded_at = 0
        self.refresh()

    def refresh(self):
        if model_registry and time.monotonic()-self.loaded_at > 60:
            self.loaded_at = time.monotonic()
            try:
                self.snapshot = model_registry.load_snapshot(home=self.home, refresh=False)
            except (OSError, ValueError, TypeError):
                pass

    @property
    def version(self):
        return self.snapshot.get('hash', 'bundled-'+PRICE_DATE) if self.snapshot else 'bundled-'+PRICE_DATE

    def price(self, usage, model):
        if model_registry and self.snapshot:
            result = model_registry.price_usage(self.snapshot, model, usage)
            if result.get('known') and result.get('usd') is not None:
                return result['usd'], result.get('credits')
            return None
        return price(usage, model)


class UsageReader:
    """Incrementally scan only the indexed rollout; retain numbers, never item bodies."""
    def __init__(self, thread_id, ledger=None, pricing=None):
        self.thread_id = thread_id
        self.identity = None
        self.offset = 0
        self.partial = b""
        self.skipping = False
        self.model = None
        self.seen = set()
        self.totals = {k: 0 for k in ("inputTokens", "cachedInputTokens", "outputTokens",
                                    "reasoningTokens", "totalTokens", "pricedTokens", "requests")}
        self.usd = self.credits = self.baseline = self.baseline_credits = 0.0
        self.priced_requests = 0
        self.skipped = 0
        self.pending = False
        self.available = False
        self.capped = False
        self.turn = {}
        self.ledger = ledger
        self.pricing = pricing
        self.credit_requests = 0
        self.reference_priced_tokens = 0
        self.persistence_error = False
        if ledger:
            for key, value in ledger.restore(thread_id).items():
                setattr(self, key, value)
            self._restore_totals()

    def _restore_totals(self):
        totals = self.ledger.totals(self.thread_id)
        for key in self.totals:
            self.totals[key] = totals[key]
        self.priced_requests, self.credit_requests = totals['pricedRequests'], totals['creditRequests']
        self.usd, self.credits = totals['usd'], totals['credits']
        self.baseline, self.baseline_credits = totals['baseline'], totals['baselineCredits']
        self.reference_priced_tokens = totals['referencePricedTokens']

    def read(self, path, budget=CHUNK):
        if not self.ledger:
            return self._read(path, budget)
        # A failed write must not advance the cursor past uncommitted usage.
        previous = {key: copy.deepcopy(getattr(self, key)) for key in
                    ('identity', 'offset', 'partial', 'skipping', 'model', 'skipped', 'turn')}
        try:
            count = self._read(path, budget)
            self._restore_totals()
            self.persistence_error = False
            return count
        except (OSError, sqlite3.Error):
            self.ledger.db.rollback()
            for key, value in previous.items():
                setattr(self, key, value)
            self._restore_totals()
            self.persistence_error = True
            raise

    def _read(self, path, budget=CHUNK):
        stat = path.stat()
        identity = (str(path), stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.identity, self.offset, self.partial = identity, 0, b""
            self.skipping, self.model = False, None
        with path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(min(budget, CHUNK))
            self.offset = stream.tell()
        self.available = True
        self.pending = self.offset < stat.st_size
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            if self.skipping:
                self.skipping = False
                continue
            if len(line) > MAX_LINE:
                self.skipped += 1
                continue
            # Only decode the two metadata event kinds; never load reasoning/tool items.
            if not any(tag in line for tag in (b'"token_usage_record"', b'"turn_context"',
                                               b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                continue
            try:
                event = json.loads(line)
                self.event(event)
            except (ValueError, TypeError, AttributeError):
                self.skipped += 1
        if len(self.partial) > MAX_LINE:
            self.partial = b""
            self.skipping = True
            self.skipped += 1
        if self.ledger:
            self.ledger.checkpoint(self.thread_id, self)
        return len(chunk)

    def event(self, event):
        kind, p = event.get("type"), event.get("payload", {})
        try:
            when = datetime.datetime.fromisoformat(event.get("timestamp", "").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            when = 0
        if kind == "event_msg":
            state = {"task_started": "inProgress", "task_complete": "completed",
                     "turn_aborted": "interrupted"}.get(p.get("type"))
            if state:
                if p.get("turn_id") != self.turn.get("turn_id"):
                    self.turn["verified"] = False
                self.turn.update(status=state, updated=when, turn_id=p.get("turn_id"))
                if state == "inProgress":
                    self.turn["started"] = when
            return
        if kind == "turn_context":
            self.model = p.get("model") if isinstance(p.get("model"), str) else None
            return
        if kind != "token_usage_record" or p.get("thread_id") != self.thread_id:
            return
        rid = p.get("response_id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 256 or rid in self.seen:
            return
        if self.ledger and self.ledger.contains(self.thread_id, rid):
            return
        usage = normalize_usage(p.get("usage"))
        if usage is None:
            self.skipped += 1
            return
        if not self.ledger and len(self.seen) >= MAX_IDS:
            self.capped = True
            return
        model = p.get('model') or self.model
        pricing = self.pricing.price if self.pricing else price
        result = pricing(usage, model) if isinstance(model, str) else None
        baseline = pricing(usage, 'gpt-6-astra') if result else None
        if self.ledger:
            turn_id = p.get('turn_id')
            if not isinstance(turn_id, str) or len(turn_id) > 256:
                turn_id = None
            if not self.ledger.add(self.thread_id, rid, turn_id, model if isinstance(model, str) else None,
                                   usage, result, baseline,
                                   self.pricing.version if self.pricing else 'bundled-'+PRICE_DATE, when):
                return
        else:
            self.seen.add(rid)
        if self.turn.get("status") == "inProgress" and p.get("turn_id") == self.turn.get("turn_id"):
            self.turn["verified"] = True
            self.turn["updated"] = max(self.turn.get("updated", 0), when)
        for out, source in (("inputTokens", "input_tokens"), ("cachedInputTokens", "cached_input_tokens"),
                            ("outputTokens", "output_tokens"), ("reasoningTokens", "reasoning_output_tokens"),
                            ("totalTokens", "total_tokens")):
            self.totals[out] += usage[source]
        self.totals["requests"] += 1
        if result:
            self.usd += result[0]
            self.baseline += baseline[0] if baseline else 0
            if result[1] is not None and baseline and baseline[1] is not None:
                self.credits += result[1]
                self.baseline_credits += baseline[1]
                self.credit_requests += 1
                self.reference_priced_tokens += usage['total_tokens']
            self.totals["pricedTokens"] += usage["total_tokens"]
            self.priced_requests += 1

    def summary(self):
        coverage = "当前日志片段 · " + ("索引中" if self.pending else "已读到末尾")
        if not self.available:
            coverage = "等待本机用量记录"
        if self.skipped:
            coverage += " · 有不可读记录"
        if self.capped:
            coverage += " · 达到计量上限"
        if self.totals["requests"] > self.priced_requests:
            coverage += " · 部分请求模型/价格未知"
        if self.persistence_error:
            coverage += ' · 本地账本暂不可写'
        current = None
        if self.ledger and self.turn.get('turn_id'):
            values = self.ledger.totals(self.thread_id, self.turn['turn_id'])
            current = {'turnId': self.turn['turn_id'], 'totalTokens': values['totalTokens'],
                       'requests': values['requests'],
                       'costUSD': values['usd'] if values['pricedRequests'] else None,
                       'partial': values['requests'] != values['pricedRequests'] or self.pending}
        return {**self.totals, "costUSD": self.usd if self.priced_requests else None,
                "credits": self.credits if self.credit_requests else None,
                "baselineUSD": self.baseline if self.priced_requests else None,
                "savingUSD": self.baseline-self.usd if self.priced_requests else None,
                "baselineCredits": self.baseline_credits if self.credit_requests else None,
                "savingCredits": self.baseline_credits-self.credits if self.credit_requests else None,
                "referencePricedTokens": self.reference_priced_tokens,
                "available": self.available,
                "partial": (not self.available or not self.totals["requests"] or self.pending or bool(self.partial) or
                            bool(self.skipped) or self.capped or
                            self.persistence_error or self.totals['requests'] > self.credit_requests or
                            self.totals["requests"] > self.priced_requests),
                "coverage": coverage, "source": "本机逐请求usage，按response_id去重并核对thread_id",
                "priceDate": '逐请求按首次计量快照冻结' if self.ledger else PRICE_DATE,
                "priceBasis": "API Standard短上下文参考价；credits为Codex Standard参考费率。非订阅账单；速度、长上下文及工具差价未计。",
                "tokenSavingStatus": "同量全Astra参考额度比价；Astra等价token，不代表实际少用token",
                "observedTurn": dict(self.turn), 'currentTurn': current,
                'persistent': self.ledger is not None,
                'priceSnapshot': self.pricing.version if self.pricing else 'bundled-'+PRICE_DATE}


def aggregate_savings(agents):
    """Compare measured, priced usage with the same usage entirely on Astra.

    Each visible thread contributes once, including descendants up to the UI's
    32-level limit. Unknown models never contribute a zero cost to the comparison.
    """
    by_id, children = {}, {}
    for agent in agents:
        tid = agent["id"]
        if tid in by_id:
            continue
        by_id[tid] = agent
        children.setdefault(agent.get("parent", ""), []).append(tid)
    results = {}
    for root in by_id:
        seen, stack = {root}, [(root, 0)]
        truncated = False
        while stack:
            parent, depth = stack.pop()
            for child in children.get(parent, []):
                if child in seen:
                    continue
                if depth >= 32:
                    truncated = True
                    continue
                seen.add(child)
                stack.append((child, depth + 1))
        baseline = credits = baseline_usd = usd = 0.0
        priced_tokens = total_tokens = missing = 0
        partial = truncated
        for tid in sorted(seen):
            usage = by_id[tid].get("usage") or {}
            total_tokens += usage.get("totalTokens", 0)
            unavailable = not usage.get("available", False) or not usage.get("requests", 0)
            missing += int(usage.get("baselineCredits") is None)
            partial = partial or unavailable or usage.get("partial", True)
            # Include exactly the fragments for which both actual and reference
            # prices are known; other measured tokens remain in totalTokens.
            if usage.get("baselineCredits") is not None and usage.get("credits") is not None:
                baseline += usage["baselineCredits"]
                credits += usage["credits"]
                baseline_usd += usage["baselineUSD"]
                usd += usage["costUSD"]
                priced_tokens += usage.get("referencePricedTokens", usage.get("pricedTokens", 0))
        saved = baseline - credits if baseline > 0 else None
        percent = saved / baseline * 100 if saved is not None else None
        results[root] = {
            "equivalentTokens": math.floor(priced_tokens * percent / 100) if percent is not None else None,
            "percent": percent, "savedCredits": saved,
            "savedUSD": baseline_usd - usd if baseline > 0 else None,
            "pricedTokens": priced_tokens, "totalTokens": total_tokens,
            "taskCount": len(seen), "missingTaskCount": missing,
            "includesChildren": len(seen) > 1, "partial": bool(partial),
            "basis": "本机已计量且有价格的usage，对比同量全Astra的Codex Standard参考额度；Astra等价token非实际少用token。",
        }
    return results


class Meter:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.readers = {}
        self.cursor = 0
        self.pricing = PricingCatalog(self.home)
        try:
            self.ledger = UsageLedger(self.home / 'adaptive-router/usage.sqlite3')
        except (OSError, sqlite3.Error):
            self.ledger = None

    def refresh(self, records):
        self.pricing.refresh()
        ids = {r["id"] for r in records}
        self.readers = {k:v for k,v in self.readers.items() if k in ids}
        for r in records:
            if r['id'] not in self.readers:
                self.readers[r['id']] = UsageReader(r['id'], self.ledger, self.pricing)
        # Eight most recent plus rotating historical tasks: at most 2MiB per refresh.
        order = records[:8]
        older = records[8:]
        if older:
            start = self.cursor % len(older)
            order += (older[start:] + older[:start])[:8]
            self.cursor = (start + 8) % len(older)
        budget = 2 * 1024 * 1024
        per_reader = max(1, budget // max(1, len(order)))
        for r in order:
            if budget <= 0:
                break
            raw = r.get("rollout_path")
            if not raw:
                self.readers[r["id"]].available = False
                continue
            try:
                path = Path(raw).resolve()
                if self.home not in path.parents or not path.is_file():
                    self.readers[r["id"]].available = False
                    continue
                budget -= self.readers[r["id"]].read(path, min(budget, per_reader))
            except (OSError, sqlite3.Error):
                self.readers[r["id"]].available = False
                self.readers[r['id']].persistence_error = True
        return {tid: reader.summary() for tid, reader in self.readers.items()}
