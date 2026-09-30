"""Deterministic independent acceptance checks, never copied into model workspaces."""
import copy
import itertools
import json
import random
from pathlib import Path

VERSION = "grader-v1.0.0"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def rejects(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return
    raise AssertionError("invalid input did not raise ValueError")


def exact_edit(files):
    original = (Path(__file__).parent / "fixtures/v1/config.ini").read_text()
    check(files["config.ini"] == original.replace("retry_count = 3  #", "retry_count = 5  #", 1),
          "section edit or byte preservation failed")


def structured_extract(files):
    # Independently fixed expected answer, not produced by the submission.
    expected = {"invoices": [{"id": "A", "customer": "Maple", "amount": "10.10"},
                              {"id": "E", "customer": "Pine", "amount": "1.20"},
                              {"id": "F", "customer": "Maple", "amount": "1.05"}],
                "totals": [{"customer": "Maple", "amount": "11.15"},
                           {"customer": "Pine", "amount": "1.20"}]}
    check(json.loads(files["summary.json"]) == expected, "latest revision/filter/order/decimal totals failed")


def routine_code(module):
    fn = module["union_ranges"]
    cases = [([], []), ([[0, 0]], []), ([[5, 8], [1, 3], [3, 5]], [[1, 8]]),
             ([[-5, -2], [-3, 0], [2, 3], [3, 3]], [[-5, 0], [2, 3]]),
             ([[2, 9], [3, 4], [2, 9]], [[2, 9]])]
    rng = random.Random(93031)
    for _ in range(40):
        values = [sorted([rng.randrange(-12, 13), rng.randrange(-12, 13)]) for _ in range(12)]
        # Cell-set oracle is independent of a sort/merge implementation.
        cells = {x for lo, hi in values for x in range(lo, hi)}
        expected = []
        for _, group in itertools.groupby(enumerate(sorted(cells)), lambda v: v[1] - v[0]):
            block = [v for _, v in group]
            expected.append([block[0], block[-1] + 1])
        cases.append((values, expected))
    for data, expected in cases:
        before = copy.deepcopy(data)
        check(fn(data) == expected, "range union mismatch")
        check(data == before, "mutated input")
    for bad in ([[1, True]], [[3, 2]], [[1]], [[1, 2, 3]], [[1.0, 2]], [None], None):
        rejects(fn, bad)


def bug_fix(module):
    cls = module["Cache"]
    for invalid in (0, -1, True, 1.0, "2"):
        rejects(cls, invalid)
    cache = cls(2)
    cache.put("a", 1, 0, 10)
    cache.put("b", 2, 0, 10)
    check(cache.get("a", 1) == 1, "get failed")
    cache.put("c", 3, 1, 10)
    try:
        cache.get("b", 1)
        raise AssertionError("did not evict LRU")
    except KeyError:
        pass
    cache.put("a", 4, 2, 10)
    check(cache.get("c", 2) == 3, "updating existing key evicted other")
    cache.put("a", 5, 3, 0)
    try:
        cache.get("a", 3)
        raise AssertionError("ttl zero stored value")
    except KeyError:
        pass
    cache = cls(2)
    cache.put("live", 1, 0, 20)
    cache.put("expired", 2, 0, 2)
    cache.put("new", 3, 2, 5)
    check(cache.get("live", 2) == 1, "expired key caused eviction of live key")
    try:
        cache.get("new", 7)
        raise AssertionError("expiry boundary failed")
    except KeyError:
        pass
    # A list-based reference tracks exact randomized operations independently.
    rng = random.Random(93032)
    actual, reference, now = cls(3), [], 0
    for _ in range(150):
        now += rng.choice([0, 0, 1])
        reference = [v for v in reference if v[2] > now]
        key = rng.choice("abcde")
        old = next((v for v in reference if v[0] == key), None)
        if rng.randrange(3):
            ttl, value = rng.randrange(5), rng.randrange(100)
            actual.put(key, value, now, ttl)
            reference = [v for v in reference if v[0] != key]
            if ttl:
                if len(reference) >= 3:
                    reference.pop(0)
                reference.append((key, value, now + ttl))
        else:
            try:
                value = actual.get(key, now)
            except KeyError:
                check(old is None, "unexpected missing live key")
            else:
                check(old is not None and value == old[1], "wrong cached value")
                reference.remove(old)
                reference.append(old)


def schedule_oracle(jobs):
    best = (0, 0, ())
    for mask in range(1 << len(jobs)):
        selected = sorted([j for i, j in enumerate(jobs) if mask & (1 << i)],
                          key=lambda j: (j["start"], j["end"], j["id"]))
        if any(a["end"] > b["start"] for a, b in zip(selected, selected[1:])):
            continue
        candidate = (-sum(j["reward"] for j in selected), len(selected), tuple(j["id"] for j in selected))
        if candidate < best:
            best = candidate
    return list(best[2])


def weighted_schedule(module):
    fn, rng = module["select_jobs"], random.Random(93033)
    cases = [[], [{"id": "z", "start": 0, "end": 1, "reward": 0}],
             [{"id": "z", "start": 0, "end": 3, "reward": 2},
              {"id": "a", "start": 0, "end": 1, "reward": 1},
              {"id": "b", "start": 1, "end": 3, "reward": 1}]]
    for _ in range(30):
        data = []
        for i in range(9):
            start = rng.randrange(-4, 9)
            data.append({"id": chr(97 + i), "start": start, "end": start + rng.randrange(1, 6),
                         "reward": rng.randrange(-2, 6)})
        rng.shuffle(data)
        cases.append(data)
    for data in cases:
        before = copy.deepcopy(data)
        check(fn(data) == schedule_oracle(data), "optimal reward or deterministic tie failed")
        check(data == before, "mutated input")
    large = [{"id": "%03d" % i, "start": i, "end": i + 1, "reward": 1} for i in range(200)]
    check(fn(large[::-1]) == [j["id"] for j in large], "large case failed")
    valid = {"id": "a", "start": 0, "end": 1, "reward": 1}
    for bad in ([valid, valid], [{**valid, "end": 0}], [{**valid, "start": True}],
                [{**valid, "reward": 1.0}], [{**valid, "id": ""}], [{**valid, "extra": 1}], [None], None):
        rejects(fn, bad)


def circular_boundaries(module):
    fn, rng = module["owner"], random.Random(93034)
    for _ in range(30):
        period = rng.randrange(1, 16)
        segs = [{"id": chr(97 + i), "start": rng.randrange(period), "end": rng.randrange(period),
                 "priority": rng.randrange(-1, 3)} for i in range(7)]
        before = copy.deepcopy(segs)
        for t in range(-period, 2 * period + 1):
            # Enumerating discrete points gives an independent wrap/boundary oracle.
            options = []
            for s in segs:
                length = (s["end"] - s["start"]) % period or period
                points = {(s["start"] + k) % period for k in range(length)}
                if t % period in points:
                    options.append((-s["priority"], length, s["id"]))
            expected = min(options)[2] if options else None
            check(fn(period, segs, t) == expected, "wrap/full-cycle/boundary/tie failed")
        check(segs == before, "mutated input")
    check(fn(5, [], -1) is None, "empty segments failed")
    valid = {"id": "a", "start": 0, "end": 1, "priority": 1}
    for period, segs, t in ((0, [], 0), (True, [], 0), (5, [], True),
                             (5, [valid, valid], 0), (5, [{**valid, "end": 5}], 0),
                             (5, [{**valid, "start": -1}], 0), (5, [{**valid, "priority": False}], 0),
                             (5, [{**valid, "id": ""}], 0), (5, [{**valid, "extra": 0}], 0),
                             (5, [None], 0), (5, None, 0)):
        rejects(fn, period, segs, t)


CHECKS = {name: value for name, value in globals().copy().items()
          if name in ("exact_edit", "structured_extract", "routine_code", "bug_fix",
                      "weighted_schedule", "circular_boundaries")}
