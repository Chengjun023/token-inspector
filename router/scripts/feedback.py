"""Validated acceptance records and anonymous sufficient statistics."""
import math
import re

STATUSES = ("accepted", "rejected", "unknown")
FAILURES = ("quality", "tool", "availability", "timeout", "cost", "latency", "cancelled", "unknown")
SOURCES = ("user", "automated_tests", "reviewer", "runtime", "unknown")
SCOPES = ("production", "benchmark")
ID = re.compile(r'^[A-Za-z0-9_.:/-]{1,200}$')


def identifier(value, name):
    if value is not None and (not isinstance(value, str) or not ID.fullmatch(value)):
        raise ValueError(name + " must be an opaque identifier (1–200 characters)")
    return value


def outcome(status, failure=None, actual_model=None, actual_effort=None, usage=None,
            cost=None, latency_ms=None, verification_source="unknown", scope="production",
            evidence_id=None):
    if status not in STATUSES or failure not in (None,) + FAILURES or verification_source not in SOURCES or scope not in SCOPES:
        raise ValueError("invalid outcome classification, source or scope")
    if status == "accepted" and (failure is not None or verification_source not in ("user", "automated_tests", "reviewer")):
        raise ValueError("acceptance requires explicit user, test or reviewer verification and no failure")
    if status == "rejected" and failure is None:
        raise ValueError("rejection requires a failure classification")
    if status == "unknown" and failure not in (None, "unknown", "cancelled", "availability", "timeout"):
        raise ValueError("unknown outcome cannot assert a quality result")
    if actual_effort is not None and actual_effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("invalid actual effort")
    for value, name in ((actual_model, "actual_model"), (evidence_id, "evidence_id")):
        identifier(value, name)
    usage = usage or {}
    if not isinstance(usage, dict) or set(usage) - {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}:
        raise ValueError("usage supports numeric token counters only")
    for value in usage.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("usage counters must be nonnegative integers")
    for value, name in ((cost, "cost"), (latency_ms, "latency_ms")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError(name + " must be finite and nonnegative")
    return {"status": status, "failure": failure, "actual_model": actual_model,
            "actual_effort": actual_effort, "usage": dict(usage), "cost_usd": cost,
            "latency_ms": latency_ms, "verification_source": verification_source,
            "scope": scope, "evidence_id": evidence_id}


def install(db):
    # Receipts outlive decision pruning to make repeated feedback idempotent.
    db.execute("CREATE TABLE IF NOT EXISTS outcome_receipts (decision_id TEXT PRIMARY KEY, thread TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS route_stats (cohort TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0, cost_sum REAL NOT NULL DEFAULT 0, cost_count INTEGER NOT NULL DEFAULT 0, latency_sum REAL NOT NULL DEFAULT 0, latency_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(cohort,model,effort))")


def accumulate(db, cohort, model, effort, result):
    if result["scope"] != "production":
        return
    db.execute("INSERT OR IGNORE INTO route_stats(cohort,model,effort) VALUES(?,?,?)", (cohort, model, effort))
    status = result["status"]  # strictly validated enum, never user SQL
    db.execute("UPDATE route_stats SET " + status + "=" + status + "+1, cost_sum=cost_sum+?, cost_count=cost_count+?, latency_sum=latency_sum+?, latency_count=latency_count+? WHERE cohort=? AND model=? AND effort=?",
               (result["cost_usd"] or 0, int(result["cost_usd"] is not None), result["latency_ms"] or 0, int(result["latency_ms"] is not None), cohort, model, effort))


def history(db, cohort):
    rows = db.execute("SELECT model,effort,accepted,rejected,unknown,cost_sum,cost_count,latency_sum,latency_count FROM route_stats WHERE cohort=?", (cohort,)).fetchall()
    return {r[0] + ":" + r[1]: {"accepted": r[2], "rejected": r[3], "unknown": r[4],
                               "mean_cost_usd": r[5]/r[6] if r[6] else None,
                               "mean_latency_ms": r[7]/r[8] if r[8] else None} for r in rows}
