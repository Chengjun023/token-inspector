#!/usr/bin/env python3
"""Bounded public model metadata; offline reads and immutable price snapshots.

This module never loads credentials, invokes a model, or executes feed contents.
All execution eligibility must also intersect the caller's live capabilities.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "adaptive-router.model-registry"
SCHEMA_VERSION = 1
FEED_URL = "https://models.dev/api.json"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_MODELS = 16384
DEFAULT_TTL = 86400
FAILURE_COOLDOWN = 900
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
TRANSIENT = frozenset(("cache_status", "stale", "error", "cache_path", "last_attempt_at"))
BUNDLE = Path(__file__).resolve().parents[1] / "data" / "models.bundled.json"


class RegistryError(ValueError):
    pass


def _now(now=None):
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now):
        return dt.datetime.fromtimestamp(now, dt.timezone.utc)
    if isinstance(now, str):
        try:
            now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistryError("无效时间") from exc
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise RegistryError("时间必须包含 timezone")
    return now.astimezone(dt.timezone.utc)


def _stamp(now=None):
    return _now(now).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value, field, limit=512):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 for c in value):
        raise RegistryError("无效字符串字段：" + field)
    return value


def _number(value, field, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1e15:
        raise RegistryError("数值必须有限、非负：" + field)
    return value


def _efforts(raw):
    if not isinstance(raw, list) or len(raw) > len(EFFORTS) or any(not isinstance(x, str) or x not in EFFORTS for x in raw) or len(set(raw)) != len(raw):
        raise RegistryError("无效 reasoning efforts")
    return list(raw)


def _canonical(snapshot):
    return {k: v for k, v in snapshot.items() if k != "hash" and k not in TRANSIENT}


def snapshot_hash(snapshot):
    body = json.dumps(_canonical(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sealed(snapshot):
    snapshot = copy.deepcopy(_canonical(snapshot))
    snapshot["hash"] = snapshot_hash(snapshot)
    return snapshot


def _json_bytes(data):
    try:
        return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise RegistryError("对象不能编码为有限 JSON") from exc


def _decode(raw, max_bytes=MAX_JSON_BYTES):
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise RegistryError("JSON 超出大小限制")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RegistryError("JSON 包含重复字段")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(RegistryError("JSON 含非有限数值")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RegistryError("无效 JSON：" + type(exc).__name__) from exc


def _read(path, max_bytes=MAX_JSON_BYTES):
    with Path(path).open("rb") as stream:
        return _decode(stream.read(max_bytes + 1), max_bytes)


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise RegistryError("不支持的注册表 schema/version")
    _text(snapshot.get("version"), "version")
    _now(snapshot.get("fetched_at"))
    if not isinstance(snapshot.get("source"), dict):
        raise RegistryError("source 必须是对象")
    rows = snapshot.get("models")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MODELS:
        raise RegistryError("无效模型数量")
    seen = set()
    for model in rows:
        if not isinstance(model, dict):
            raise RegistryError("model 必须是对象")
        key = (_text(model.get("provider"), "provider"), _text(model.get("id"), "id"))
        if key in seen:
            raise RegistryError("重复模型身份")
        seen.add(key)
        if model.get("status") not in ("verified", "candidate", "admitted", "disabled"):
            raise RegistryError("无效模型状态")
        cap = model.get("capabilities")
        if not isinstance(cap, dict) or not isinstance(cap.get("verified"), bool):
            raise RegistryError("缺少能力验证状态")
        _efforts(cap.get("efforts"))
        for field in ("tools", "reasoning"):
            if cap.get(field) is not None and not isinstance(cap[field], bool):
                raise RegistryError("能力字段必须为 bool 或 null")
        _number(cap.get("context_window"), "context_window")
        if cap.get("context_window") is not None and type(cap["context_window"]) is not int:
            raise RegistryError("context_window 必须为整数或 null")
        for field in ("input_modalities", "output_modalities"):
            items = cap.get(field, [])
            if not isinstance(items, list) or len(items) > 16:
                raise RegistryError("无效 modalities")
            for item in items:
                _text(item, field, 64)
        endpoints = model.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) > 64:
            raise RegistryError("无效 endpoints")
        endpoint_keys = set()
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise RegistryError("endpoint 必须是对象")
            endpoint_key = (_text(endpoint.get("provider"), "endpoint.provider"), _text(endpoint.get("id"), "endpoint.id"))
            if endpoint_key in endpoint_keys:
                raise RegistryError("重复 endpoint")
            endpoint_keys.add(endpoint_key)
            if endpoint.get("kind") not in ("native", "api") or not isinstance(endpoint.get("verified"), bool):
                raise RegistryError("无效 endpoint 验证状态")
        pricing = model.get("pricing")
        if not isinstance(pricing, dict) or not isinstance(pricing.get("verified"), bool):
            raise RegistryError("无效 pricing")
        for kind in ("usd_per_million", "credits_per_million"):
            rates = pricing.get(kind)
            if not isinstance(rates, dict) or set(rates) - {"input", "cached_input", "cache_write_input", "output"}:
                raise RegistryError("无效价格字段")
            for field, value in rates.items():
                _number(value, kind + "." + field)
        routing = model.get("routing", {})
        if not isinstance(routing, dict) or not isinstance(routing.get("strengths", {}), dict):
            raise RegistryError("无效 routing 对象")
        if routing.get("tier") not in (None, "frontier", "standard", "light"):
            raise RegistryError("无效 routing tier")
        _number(routing.get("latency_ms"), "latency_ms")
        for field in ("verified", "latency_verified"):
            if not isinstance(routing.get(field, False), bool):
                raise RegistryError("routing 验证字段必须为 bool")
        for value in routing.get("strengths", {}).values():
            if _number(value, "strength", False) > 1:
                raise RegistryError("strength 必须介于 0 和 1")
        if not isinstance(routing.get("safety_equivalent_verified", False), bool):
            raise RegistryError("无效 safety equivalence")
    if snapshot.get("hash") != snapshot_hash(snapshot):
        raise RegistryError("注册表 hash 校验失败")
    return snapshot


def read_snapshot(path):
    """Read a frozen, hash-checked local snapshot without refresh or policy overlay."""
    return copy.deepcopy(validate_snapshot(_read(path, MAX_SNAPSHOT_BYTES)))


def bundled_snapshot():
    return read_snapshot(BUNDLE)


def _root(home=None):
    return Path(home or os.environ.get("ADAPTIVE_ROUTER_HOME", Path.home() / ".codex/adaptive-router")) / "model-registry"


def _atomic(path, raw, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".registry-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(name, str(path))
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise RegistryError("禁止覆盖已有冻结快照")
        else:
            os.replace(name, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def freeze_snapshot(snapshot, path):
    """Write immutable content; an existing different file is never overwritten."""
    frozen = _sealed(snapshot)
    validate_snapshot(frozen)
    raw = _json_bytes(frozen)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise RegistryError("冻结快照超出大小限制")
    _atomic(path, raw, exclusive=True)
    return frozen["hash"]


def _persist(snapshot, home):
    root = _root(home)
    freeze_snapshot(snapshot, root / "snapshots" / (snapshot["hash"].split(":", 1)[1] + ".json"))
    _atomic(root / "lastgood.json", _json_bytes(_sealed(snapshot)))


def _local_policy(home):
    path = _root(home) / "policy.json"
    if not path.exists():
        return {"schema_version": 1, "models": {}, "host": {}}
    value = _read(path)
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("models"), dict) or not isinstance(value.get("host"), dict):
        raise RegistryError("无效本地准入策略")
    for key, admission in value["models"].items():
        _text(key, "local model identity", 1024)
        if not isinstance(admission, dict) or admission.get("status") not in ("admitted", "disabled", "verified", "candidate"):
            raise RegistryError("无效本地模型状态")
        _text(admission.get("evidence"), "evidence", 2048)
        if admission["status"] == "admitted":
            _efforts(admission.get("efforts"))
            if not isinstance(admission.get("endpoints"), list):
                raise RegistryError("缺少本地 endpoints")
    for entry in value["host"].values():
        if not isinstance(entry, dict):
            raise RegistryError("无效 host 准入")
        _text(entry.get("provider"), "host.provider")
        _text(entry.get("id"), "host.id")
        _text(entry.get("evidence"), "evidence", 2048)
        _efforts(entry.get("efforts"))
    return value


def _overlay(snapshot, home):
    policy = _local_policy(home)
    if not policy["models"]:
        return snapshot
    snapshot = copy.deepcopy(snapshot)
    for model in snapshot["models"]:
        admission = policy["models"].get(model["provider"] + "/" + model["id"])
        if admission:
            model["status"] = admission["status"]
            model["admission"] = copy.deepcopy(admission)
            if admission["status"] == "admitted":
                model["capabilities"]["efforts"] = admission["efforts"]
                model["endpoints"] = admission["endpoints"]
            model["capabilities"].update(admission.get("capabilities", {}))
            model["routing"].update(admission.get("routing", {}))
            if "pricing" in admission:
                model["pricing"] = copy.deepcopy(admission["pricing"])
    snapshot["source"]["local_policy_sha256"] = hashlib.sha256(_json_bytes(policy)).hexdigest()
    return validate_snapshot(_sealed(snapshot))


def load_snapshot(home=None, refresh=False, ttl_seconds=DEFAULT_TTL, now=None):
    """Offline by default. Explicit refresh uses TTL and failure cooldown."""
    if refresh:
        return refresh_snapshot(home, ttl_seconds=ttl_seconds, now=now)
    _number(ttl_seconds, "ttl_seconds", False)
    path = _root(home) / "lastgood.json"
    error = None
    try:
        snapshot = read_snapshot(path) if path.exists() else bundled_snapshot()
        status = "cached" if path.exists() else "bundled"
    except (OSError, RegistryError):
        snapshot, status, error = bundled_snapshot(), "cache_invalid", "缓存无效，使用内置快照"
    snapshot = _overlay(snapshot, home)
    stale = (_now(now) - _now(snapshot["fetched_at"])).total_seconds() > ttl_seconds
    attempt_path = _root(home) / "refresh-status.json"
    try:
        attempt = _read(attempt_path) if attempt_path.exists() else {}
        if attempt.get("error"):
            error, stale = attempt["error"], True
            snapshot["last_attempt_at"] = attempt.get("attempted_at")
    except (OSError, RegistryError, AttributeError):
        error, stale = "刷新状态文件无效", True
    snapshot.update(cache_status=status, stale=stale or bool(error), error=error, cache_path=str(path))
    return snapshot


def get_model(snapshot, model_id, provider="openai"):
    for model in snapshot["models"]:
        if model["provider"] == provider and model["id"] == model_id:
            return copy.deepcopy(model)
    return None


def eligible_models(snapshot, available_models, provider="codex", available_efforts=None):
    """Return admitted rows with live endpoint/effort intersections, never guesses.

    Use {endpoint_id: [efforts]}, or [{provider,id,efforts}]. A bare id list
    needs available_efforts; otherwise it cannot prove a usable model/effort.
    """
    available = {}
    if isinstance(available_models, dict):
        for model_id, efforts in available_models.items():
            available[(provider, model_id)] = _efforts(list(efforts))
    else:
        for item in available_models:
            if isinstance(item, dict):
                available[(item["provider"], item["id"])] = _efforts(item["efforts"])
            elif isinstance(item, str) and available_efforts and item in available_efforts:
                available[(provider, item)] = _efforts(list(available_efforts[item]))
    result = []
    for model in snapshot["models"]:
        if model["status"] not in ("verified", "admitted"):
            continue
        endpoints = []
        for endpoint in model["endpoints"]:
            efforts = [x for x in model["capabilities"]["efforts"] if x in available.get((endpoint["provider"], endpoint["id"]), [])]
            if endpoint["provider"] == provider and efforts and (endpoint["verified"] or model["status"] == "admitted"):
                endpoints.append(dict(endpoint, efforts=efforts))
        if endpoints:
            result.append(dict(copy.deepcopy(model), eligible_endpoints=endpoints))
    return result


def _url(url):
    _text(url, "url", 2048)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise RegistryError("仅接受不含凭据、query 或 fragment 的 HTTPS 元数据 URL")
    return url


def _feed_model(provider_id, model_id, raw, source_url, now):
    _text(model_id, "model.id")
    if not isinstance(raw, dict) or raw.get("id", model_id) != model_id:
        raise RegistryError("model id 不一致")
    cost = raw.get("cost", {})
    limit = raw.get("limit", {})
    if not isinstance(cost, dict) or not isinstance(limit, dict):
        raise RegistryError("无效 cost/limit 对象")
    def cost_fields(value, depth=0):
        if depth > 16:
            raise RegistryError("cost 嵌套层级超出限制")
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "tier":
                    if not isinstance(item, dict):
                        raise RegistryError("无效价格 tier")
                    _text(item.get("type"), "cost.tier.type", 64)
                    for tier_key, tier_value in item.items():
                        if tier_key != "type":
                            _number(tier_value, "cost.tier." + tier_key, False)
                else:
                    cost_fields(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 64:
                raise RegistryError("价格 tiers 超出限制")
            for item in value:
                cost_fields(item, depth + 1)
        else:
            _number(value, "cost", nullable=True)
    cost_fields(cost)
    rates = {}
    for source, target in (("input", "input"), ("output", "output"), ("cache_read", "cached_input"), ("cache_write", "cache_write_input")):
        rates[target] = _number(cost.get(source), "cost." + source)
    for field in ("tool_call", "reasoning"):
        if raw.get(field) is not None and not isinstance(raw[field], bool):
            raise RegistryError("能力字段必须为 bool")
    return {"provider": provider_id, "id": model_id,
                 "name": _text(raw.get("name", model_id), "name"), "status": "candidate",
                 "capabilities": {"verified": False, "efforts": [], "tools": raw.get("tool_call"),
                                  "reasoning": raw.get("reasoning"), "context_window": _number(limit.get("context"), "context"),
                                  "input_modalities": [], "output_modalities": []},
                 "endpoints": [{"provider": provider_id, "id": model_id, "kind": "api", "verified": False}],
                 "pricing": {"usd_per_million": rates, "credits_per_million": {}, "as_of": _stamp(now)[:10],
                             "source": [source_url], "verified": False, "basis": "models.dev 公共参考元数据；未验证服务等级与账单"},
                 "routing": {"tier": None, "strengths": {}, "latency_ms": None, "latency_verified": False,
                             "verified": False, "safety_equivalent_verified": False, "source": "unverified-public-metadata"}}


def convert_feed(feed, source_url=FEED_URL, now=None):
    """Convert bounded metadata; quarantine bad rows without executing anything."""
    _url(source_url)
    if not isinstance(feed, dict) or not 1 <= len(feed) <= 512:
        raise RegistryError("无效 models.dev provider feed")
    rows, warnings = [], []
    skipped, seen = 0, 0
    for provider_id, provider in feed.items():
        _text(provider_id, "provider")
        if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
            raise RegistryError("provider 缺少 models 对象")
        for model_id, raw in provider["models"].items():
            seen += 1
            if seen > MAX_MODELS - 7:
                raise RegistryError("模型数量超出限制")
            try:
                row = _feed_model(provider_id, model_id, raw, source_url, now)
                context = row["capabilities"]["context_window"]
                if context is not None and type(context) is not int:
                    raise RegistryError("context_window 必须为整数或 null")
                rows.append(row)
            except RegistryError as exc:
                skipped += 1
                if len(warnings) < 50:
                    warnings.append({"provider": provider_id, "id": str(model_id)[:512], "reason": str(exc)})
    if not rows:
        raise RegistryError("feed 没有模型")
    # Keep the verified bundled references separate from public feed claims.
    base = bundled_snapshot()
    known = {(x["provider"], x["id"]) for x in base["models"]}
    for row in rows:
        if (row["provider"], row["id"]) not in known:
            base["models"].append(row)
        else:
            match = next(x for x in base["models"] if (x["provider"], x["id"]) == (row["provider"], row["id"]))
            match["public_metadata"] = {"pricing": row["pricing"], "capabilities": row["capabilities"]}
    base["fetched_at"] = _stamp(now)
    base["version"] = "models.dev-" + hashlib.sha256(_json_bytes(feed)).hexdigest()[:16]
    base["source"] = {"kind": "models.dev", "url": source_url, "feed_sha256": hashlib.sha256(_json_bytes(feed)).hexdigest(),
                      "bundled_hash": bundled_snapshot()["hash"], "skipped_models": skipped, "input_models": seen, "warnings": warnings}
    return validate_snapshot(_sealed(base))


def import_feed(path, home=None, now=None, source_url=FEED_URL):
    snapshot = convert_feed(_read(path), source_url, now)
    _persist(snapshot, home)
    _atomic(_root(home) / "refresh-status.json", _json_bytes({"attempted_at": _stamp(now), "error": None}))
    return load_snapshot(home, now=now)


class _HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url):
    request = urllib.request.Request(_url(url), headers={"User-Agent": "adaptive-router-model-registry/1", "Accept": "application/json"})
    with urllib.request.build_opener(_HTTPSRedirects()).open(request, timeout=15) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_JSON_BYTES):
            raise RegistryError("远端 JSON 超出大小限制")
        return _decode(response.read(MAX_JSON_BYTES + 1))


def refresh_snapshot(home=None, url=FEED_URL, ttl_seconds=DEFAULT_TTL, now=None, force=False):
    current = load_snapshot(home, now=now, ttl_seconds=ttl_seconds)
    if not force and not current["stale"] and current["cache_status"] == "cached":
        return current
    last = current.get("last_attempt_at")
    if not force and last and (_now(now) - _now(last)).total_seconds() < FAILURE_COOLDOWN:
        return dict(current, cache_status="cooldown")
    try:
        snapshot = convert_feed(_download(url), url, now)
        _persist(snapshot, home)
        _atomic(_root(home) / "refresh-status.json", _json_bytes({"attempted_at": _stamp(now), "error": None}))
        return dict(load_snapshot(home, now=now, ttl_seconds=ttl_seconds), cache_status="refreshed")
    except (OSError, RegistryError, ValueError, urllib.error.URLError) as exc:
        error = "公共目录刷新失败：" + ("HTTP " + str(exc.code) if isinstance(exc, urllib.error.HTTPError)
                                      else str(exc) if isinstance(exc, RegistryError) else type(exc).__name__)
        try:
            _atomic(_root(home) / "refresh-status.json", _json_bytes({"attempted_at": _stamp(now), "error": error}))
        except OSError:
            error += "；无法持久化刷新状态"
        return dict(current, cache_status="refresh_failed", stale=True, error=error, last_attempt_at=_stamp(now))


def admit_model(model_id, provider="openai", home=None, efforts=None, endpoints=None, evidence=None,
                capabilities=None, routing=None):
    """Explicit local admission is distinct from verified benchmark capability."""
    _text(evidence, "evidence", 2048)
    if get_model(load_snapshot(home), model_id, provider) is None:
        raise RegistryError("待准入模型不在目录中，请先 import/refresh")
    efforts = _efforts(efforts or [])
    if not efforts or not endpoints:
        raise RegistryError("准入需声明 endpoint 与 efforts")
    value = {"status": "admitted", "efforts": efforts, "endpoints": endpoints,
             "evidence": evidence, "updated_at": _stamp()}
    if capabilities is not None:
        if not isinstance(capabilities, dict) or set(capabilities) - {"verified", "tools", "reasoning", "context_window", "input_modalities", "output_modalities"}:
            raise RegistryError("无效本地 capabilities 字段")
        value["capabilities"] = capabilities
    if routing is not None:
        if not isinstance(routing, dict) or set(routing) - {"verified", "tier", "strengths", "latency_ms", "latency_verified", "safety_equivalent_verified", "source"}:
            raise RegistryError("无效本地 routing 字段")
        value["routing"] = dict(routing, source="local-admission-evidence")
    policy = _local_policy(home)
    old = policy["models"].get(provider + "/" + model_id, {})
    if "pricing" in old:
        value["pricing"] = old["pricing"]
    policy["models"][provider + "/" + model_id] = value
    test = load_snapshot(home)
    for model in test["models"]:
        if (model["provider"], model["id"]) == (provider, model_id):
            model.update(status="admitted", endpoints=endpoints)
            model["capabilities"]["efforts"] = efforts
            model["capabilities"].update(capabilities or {})
            model["routing"].update(routing or {})
    validate_snapshot(_sealed(test))
    _atomic(_root(home) / "policy.json", _json_bytes(policy))
    return _archive_local_snapshot(home)


def adopt_pricing(model_id, provider="openai", home=None, evidence=None, verified=False):
    """Explicitly adopt observed public prices; existing frozen prices remain intact.

    verified=True means the local operator checked the source and service tier;
    the public feed alone cannot set it. Credits absent from feed remain unknown.
    """
    _text(evidence, "evidence", 2048)
    if not isinstance(verified, bool):
        raise RegistryError("verified 必须为 bool")
    model = get_model(load_snapshot(home), model_id, provider)
    if model is None:
        raise RegistryError("模型没有待采用的 public_metadata 价格")
    observed = model.get("public_metadata", {}).get("pricing")
    if observed is None and not model["pricing"]["verified"]:
        observed = model["pricing"]
    if observed is None:
        raise RegistryError("模型没有待采用的 public_metadata 价格")
    pricing = copy.deepcopy(observed)
    pricing["verified"] = verified
    pricing["adoption_evidence"] = evidence
    pricing["adopted_at"] = _stamp()
    # Public USD changes do not establish that Codex credits stayed unchanged.
    policy = _local_policy(home)
    key = provider + "/" + model_id
    admission = policy["models"].get(key, {"status": model["status"], "evidence": evidence, "updated_at": _stamp()})
    admission.update(pricing=pricing, evidence=evidence, updated_at=_stamp())
    policy["models"][key] = admission
    _atomic(_root(home) / "policy.json", _json_bytes(policy))
    return _archive_local_snapshot(home)


def _archive_local_snapshot(home):
    snapshot = load_snapshot(home)
    freeze_snapshot(snapshot, _root(home) / "snapshots" / (snapshot["hash"].split(":", 1)[1] + ".json"))
    return snapshot


def disable_model(model_id, provider="openai", home=None, evidence="用户本地禁用"):
    _text(evidence, "evidence", 2048)
    if get_model(load_snapshot(home), model_id, provider) is None:
        raise RegistryError("模型不在目录中")
    policy = _local_policy(home)
    key = provider + "/" + model_id
    admission = policy["models"].get(key, {})
    admission.update(status="disabled", evidence=evidence, updated_at=_stamp())
    policy["models"][key] = admission
    _atomic(_root(home) / "policy.json", _json_bytes(policy))
    return _archive_local_snapshot(home)


def host_models(home=None, provider="codex"):
    """Evidence-backed local host extensions only; callers retain their baseline."""
    result = {}
    for entry in _local_policy(home)["host"].values():
        if entry.get("provider") == provider and entry.get("evidence"):
            result[entry["id"]] = _efforts(entry["efforts"])
    return result


def admit_host(model_id, efforts, evidence, home=None, provider="codex"):
    _text(model_id, "host.id")
    _text(provider, "host.provider")
    _text(evidence, "evidence", 2048)
    efforts = _efforts(efforts)
    if not efforts:
        raise RegistryError("host 准入需明确 efforts")
    policy = _local_policy(home)
    policy["host"][provider + "/" + model_id] = {"provider": provider, "id": model_id, "efforts": efforts,
                                                 "evidence": evidence, "updated_at": _stamp()}
    _atomic(_root(home) / "policy.json", _json_bytes(policy))
    return host_models(home, provider)


def price_usage(snapshot, model_id, usage, provider="openai"):
    """Price one request against exactly this snapshot; unknown stays null."""
    values = {}
    for field in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens"):
        value = usage.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
            raise RegistryError("无效 token usage")
        values[field] = value
    i, c, w, o = values.values()
    if c + w > i:
        raise RegistryError("cached/write tokens 超过 input tokens")
    model = get_model(snapshot, model_id, provider)
    pricing = model["pricing"] if model else {}
    def total(rates, credits=False):
        terms = ((i-c, "input"), (c, "cached_input"), (o, "output")) if credits else ((i-c-w, "input"), (c, "cached_input"), (w, "cache_write_input"), (o, "output"))
        if any(count and rates.get(field) is None for count, field in terms):
            return None
        return sum(count * (rates.get(field) or 0) for count, field in terms) / 1e6
    usd = total(pricing.get("usd_per_million", {})) if model else None
    credits = total(pricing.get("credits_per_million", {}), True) if model else None
    return {"usd": usd, "credits": credits, "known": usd is not None, "credits_known": credits is not None,
            "priceVersion": pricing.get("as_of"), "snapshot_hash": snapshot["hash"],
            "provider": provider, "model_id": model_id, "price_verified": pricing.get("verified", False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--url", default=FEED_URL)
    refresh.add_argument("--force", action="store_true")
    imp = sub.add_parser("import")
    imp.add_argument("path")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("path")
    for command in ("admit", "disable"):
        cmd = sub.add_parser(command)
        cmd.add_argument("model")
        cmd.add_argument("--provider", default="openai")
        cmd.add_argument("--evidence", required=True)
        if command == "admit":
            cmd.add_argument("--efforts", required=True, help="逗号分隔")
            cmd.add_argument("--endpoint-provider", required=True)
            cmd.add_argument("--endpoint-id")
            cmd.add_argument("--endpoint-kind", choices=("native", "api"), default="api")
            cmd.add_argument("--capabilities-verified", action="store_true")
            cmd.add_argument("--tools", action="store_true", default=None)
            cmd.add_argument("--tier", choices=("frontier", "standard", "light"))
            cmd.add_argument("--routing-verified", action="store_true")
            cmd.add_argument("--safety-equivalent-verified", action="store_true")
    adopt = sub.add_parser("adopt-pricing")
    adopt.add_argument("model")
    adopt.add_argument("--provider", default="openai")
    adopt.add_argument("--evidence", required=True)
    adopt.add_argument("--verified", action="store_true")
    host = sub.add_parser("admit-host")
    host.add_argument("model")
    host.add_argument("--provider", default="codex")
    host.add_argument("--efforts", required=True)
    host.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            result = refresh_snapshot(args.home, args.url, force=args.force)
        elif args.command == "import":
            result = import_feed(args.path, args.home)
        elif args.command == "freeze":
            result = {"hash": freeze_snapshot(load_snapshot(args.home), args.path), "path": str(Path(args.path).resolve())}
        elif args.command == "admit":
            endpoint = {"provider": args.endpoint_provider, "id": args.endpoint_id or args.model, "kind": args.endpoint_kind, "verified": False}
            capabilities = {"verified": args.capabilities_verified}
            if args.tools is not None:
                capabilities["tools"] = args.tools
            routing = {"tier": args.tier, "verified": args.routing_verified, "safety_equivalent_verified": args.safety_equivalent_verified}
            result = admit_model(args.model, args.provider, args.home, args.efforts.split(","), [endpoint], args.evidence, capabilities, routing)
        elif args.command == "disable":
            result = disable_model(args.model, args.provider, args.home, args.evidence)
        elif args.command == "adopt-pricing":
            result = adopt_pricing(args.model, args.provider, args.home, args.evidence, args.verified)
        elif args.command == "admit-host":
            result = admit_host(args.model, args.efforts.split(","), args.evidence, args.home, args.provider)
        else:
            result = load_snapshot(args.home)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if isinstance(result, dict) and result.get("cache_status") == "refresh_failed" else 0
    except (OSError, RegistryError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
