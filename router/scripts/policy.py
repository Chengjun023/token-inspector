"""Pure local ranking: no state reads, network calls or model invocation."""
import hashlib
import json
import math

PROFILES = ("quality", "balanced", "economy", "speed")
WORKLOADS = ("read", "write", "code", "research", "mixed")
MIN_SAMPLES = 5
VERSION = "1.0"


def preference_answers(priority=None, workload=None):
    """Two small questions map to preferences; never mutate explicit phase pins."""
    priorities = {"accuracy": "quality", "quality": "quality", "balance": "balanced",
                  "balanced": "balanced", "cost": "economy", "economy": "economy",
                  "latency": "speed", "speed": "speed"}
    if priority is not None and priority not in priorities:
        raise ValueError("invalid priority answer")
    if workload is not None and workload not in WORKLOADS:
        raise ValueError("invalid workload answer")
    return {k: v for k, v in (("profile", priorities.get(priority)), ("workload", workload)) if v}


def workload_for(kind):
    return {"read": "read", "extract": "read", "edit": "write", "code": "code",
            "debug": "code", "architecture": "code", "reason": "research"}.get(kind, "mixed")


def cohort(difficulty, phase, workload):
    score = difficulty["score"]
    band = "light" if score < 4.5 else "ordinary" if score < 7 else "demanding"
    return ":".join((difficulty["task_type"], phase, workload, band))


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _metadata(snapshot):
    result = {}
    for item in (snapshot or {}).get("models", []):
        # The CLI endpoint is separate from the API model identity.
        for ep in item.get("endpoints", []):
            if ep.get("provider") == "codex" and (ep.get("verified") or item.get("status") == "admitted"):
                result[ep.get("id")] = item
    return result


def rank(prior_pairs, models, available, snapshot=None, profile="balanced", workload="mixed",
         constraints=None, history=None, require_safety=False, required_tier="light"):
    """Rank only advertised, host-authorized, admitted pairs.

    Legacy order is a labeled uncalibrated prior. Verified catalog metadata and
    production acceptance counts can change that order. Missing data is neutral.
    """
    if profile not in PROFILES or workload not in WORKLOADS:
        raise ValueError("invalid profile or workload")
    if history is not None and not isinstance(history, dict):
        raise ValueError("history must be a mapping")
    constraints = constraints or {}
    if set(constraints) - {"context_tokens", "tools", "available_tools"}:
        raise ValueError("unknown routing constraint")
    context = constraints.get("context_tokens", 0)
    if not isinstance(context, int) or isinstance(context, bool) or context < 0:
        raise ValueError("context_tokens must be a nonnegative integer")
    tools = constraints.get("tools", [])
    if not isinstance(tools, (bool, list, tuple)) or (not isinstance(tools, bool) and any(not isinstance(t, str) for t in tools)):
        raise ValueError("tools must be a bool or list of names")
    available_tools = constraints.get("available_tools", [])
    if not isinstance(available_tools, (list, tuple)) or any(not isinstance(t, str) for t in available_tools):
        raise ValueError("available_tools must be verified host tool names")
    if isinstance(tools, (list, tuple)) and not set(tools).issubset(set(available_tools)):
        raise ValueError("required tool names lack host capability evidence")
    metadata = _metadata(snapshot)
    tier_level = {"light": 0, "standard": 1, "frontier": 2}
    if required_tier not in tier_level:
        raise ValueError("invalid required tier")
    candidates, excluded = [], []
    known_pairs = list(dict.fromkeys(prior_pairs))
    # New models need both registry admission and explicit host proof. A directory
    # listing alone cannot make them candidates. A comparable tier is required.
    preferred_effort = prior_pairs[0][1] if prior_pairs else "medium"
    for model in sorted(set(models) & set(available)):
        item = metadata.get(model)
        if not item or item.get("status") not in ("verified", "admitted"):
            continue
        routing = item.get("routing", {})
        if routing.get("tier") not in ("light", "standard", "frontier"):
            continue
        if tier_level[routing["tier"]] < tier_level[required_tier]:
            continue
        if not any(m == model for m, _ in known_pairs):
            levels = set(models[model]) & set(item.get("capabilities", {}).get("efforts", []))
            if require_safety and preferred_effort not in levels:
                continue
            effort = preferred_effort if preferred_effort in levels else next((e for e in ("high", "medium", "low") if e in levels), None)
            if effort:
                known_pairs.append((model, effort))
    for index, (model, effort) in enumerate(known_pairs):
        if model not in available or effort not in models.get(model, []):
            continue
        item = metadata.get(model)
        if snapshot and (not item or item.get("status") not in ("verified", "admitted") or not item.get("capabilities", {}).get("verified")):
            excluded.append({"model": model, "reason": "unadmitted_or_unverified"})
            continue
        caps = (item or {}).get("capabilities", {})
        routing = (item or {}).get("routing", {})
        if snapshot and effort not in caps.get("efforts", []):
            excluded.append({"model": model, "reason": "registry_effort_unsupported"})
            continue
        if require_safety and not (model == "gpt-6-astra" or routing.get("safety_equivalent_verified") is True):
            continue
        if context and (not caps.get("verified") or not isinstance(caps.get("context_window"), int) or caps["context_window"] < context):
            excluded.append({"model": model, "reason": "context_unknown_or_insufficient"})
            continue
        if tools and (not caps.get("verified") or caps.get("tools") is not True):
            excluded.append({"model": model, "reason": "tools_unknown_or_unsupported"})
            continue
        reasons = ["legacy_order_uncalibrated"]
        score = 100.0 - min(index, 10) * 5.0
        strength = routing.get("strengths", {}).get(workload)
        if routing.get("verified") and _finite(strength) and strength <= 1:
            score += strength * (40 if profile == "quality" else 20)
            reasons.append("verified_workload_strength")
        pricing = (item or {}).get("pricing", {})
        rates = pricing.get("usd_per_million", {})
        cost = rates.get("input") + rates.get("output") if _finite(rates.get("input")) and _finite(rates.get("output")) else None
        latency = routing.get("latency_ms") if routing.get("latency_verified") and _finite(routing.get("latency_ms")) else None
        stats = (history or {}).get(model + ":" + effort, {})
        if not isinstance(stats, dict) or any(isinstance(stats.get(k, 0), bool) or not isinstance(stats.get(k, 0), int) or stats.get(k, 0) < 0 for k in ("accepted", "rejected", "unknown")):
            raise ValueError("invalid history counters")
        count = stats.get("accepted", 0) + stats.get("rejected", 0)
        learned = count >= MIN_SAMPLES
        if learned:
            # Smoothed bounded adjustment, not a prediction/probability claim.
            score += 30 * ((stats.get("accepted", 0) + 1) / (count + 2) - .5)
            reasons.append("production_acceptance_counts")
        candidates.append({"model": model, "reasoning_effort": effort, "rank_score": score,
                           "cost_index": cost if pricing.get("verified") else None,
                           "latency_ms": latency, "reasons": reasons,
                           "history": {"accepted": stats.get("accepted", 0), "rejected": stats.get("rejected", 0),
                                       "min_samples": MIN_SAMPLES, "used": learned},
                           "prior_calibrated": False})
    costs = [c["cost_index"] for c in candidates if c["cost_index"] is not None]
    latencies = [c["latency_ms"] for c in candidates if c["latency_ms"] is not None]
    for c in candidates:
        if len(costs) > 1 and c["cost_index"] is not None and max(costs) > min(costs):
            weight = 35 if profile == "economy" else 2 if profile == "balanced" else 0
            c["rank_score"] -= weight * (c["cost_index"] - min(costs)) / (max(costs) - min(costs))
            if weight:
                c["reasons"].append("verified_price_comparison")
        if len(latencies) > 1 and c["latency_ms"] is not None and max(latencies) > min(latencies):
            weight = 35 if profile == "speed" else 5 if profile == "balanced" else 0
            c["rank_score"] -= weight * (c["latency_ms"] - min(latencies)) / (max(latencies) - min(latencies))
            if weight:
                c["reasons"].append("verified_latency_comparison")
        c["rank_score"] = round(c["rank_score"], 4)
    candidates.sort(key=lambda c: -c["rank_score"])
    return {"version": VERSION, "profile": profile, "workload": workload,
            "calibration": "uncalibrated_prior_with_local_counts", "candidates": candidates,
            "excluded": excluded, "history_hash": digest(history or {}),
            "registry_hash": (snapshot or {}).get("hash"), "constraints": constraints,
            "price_basis": "input_plus_output_USD_per_million_comparison; no task cost forecast"}
