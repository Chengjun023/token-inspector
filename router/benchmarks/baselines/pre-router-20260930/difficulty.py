"""Small, deterministic task-difficulty assessment; no network or model calls."""

from decimal import Decimal, InvalidOperation


TASK_TYPES = ("read", "extract", "edit", "code", "debug", "architecture", "reason", "general")
BASE = {"read": 3.2, "extract": 3.4, "edit": 3.5, "code": 5.1,
        "debug": 6.0, "architecture": 7.5, "reason": 6.3, "general": 4.0}
KEYWORDS = (
    ("architecture", ("architecture", "design system", "架构", "系统设计")),
    ("debug", ("debug", "traceback", "stack trace", "排查", "调试", "故障")),
    ("code", ("implement", "refactor", "编程", "实现", "重构")),
    ("extract", ("extract", "提取", "抽取")),
    ("read", ("read", "summarize", "阅读", "总结", "查阅")),
    ("edit", ("edit", "修改", "编辑", "润色")),
    ("reason", ("prove", "analyze", "推理", "论证", "分析")),
)
UNCERTAIN = ("unknown", "unclear", "ambiguous", "uncertain", "不确定", "未知", "不清楚", "模糊")


def _score(value):
    if isinstance(value, bool):
        raise ValueError("score must be a finite number from 1.0 to 10.0 in 0.1 steps")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("score must be a finite number from 1.0 to 10.0 in 0.1 steps") from None
    if not number.is_finite() or not Decimal("1.0") <= number <= Decimal("10.0") or number * 10 != (number * 10).to_integral_value():
        raise ValueError("score must be a finite number from 1.0 to 10.0 in 0.1 steps")
    return float(number)


def assess(text='', phase='act', complexity='normal', risk='low', failures=0,
           score=None, task_type='general', source='heuristic') -> dict:
    """Return only derived features; callers must never persist ``text``."""
    if task_type not in TASK_TYPES or phase not in ("think", "act", "review"):
        raise ValueError("invalid task type or phase")
    if complexity not in ("simple", "normal", "hard") or risk not in ("low", "medium", "high"):
        raise ValueError("invalid complexity or risk")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("failures must be a nonnegative integer")
    if source not in ("heuristic", "caller_assessment"):
        raise ValueError("invalid score source")
    kind = task_type
    if kind == "general" and text:
        lower = text.lower()
        for found, terms in KEYWORDS:
            if any(term in lower for term in terms):
                kind = found
                break
    reasons = [f"task_type={kind}"]
    if score is None:
        value = BASE[kind]
        value += {"simple": -0.8, "normal": 0, "hard": 1.4}[complexity]
        value += {"low": 0, "medium": 0.7, "high": 1.7}[risk]
        value += min(failures, 2) * 0.6
        if text and any(term in text.lower() for term in UNCERTAIN):
            value += 0.8
            reasons.append("uncertainty_signal")
        if phase == "review":
            value += 0.2
        value = round(max(1.0, min(10.0, value)), 1)
        if complexity != "normal":
            reasons.append(f"complexity={complexity}")
        if risk != "low":
            reasons.append(f"risk={risk}")
        if failures:
            reasons.append(f"failures={min(failures, 2)}+")
        return {"score": value, "source": "heuristic", "confidence": "medium" if kind != "general" else "low",
                "task_type": kind, "reasons": reasons}
    value = _score(score)
    reasons.append("explicit_score")
    return {"score": value, "source": "caller_assessment" if source == "heuristic" else source,
            "confidence": "medium", "task_type": kind, "reasons": reasons}
