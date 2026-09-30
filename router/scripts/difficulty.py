"""Deterministic action/constraint assessment. No task text is retained."""
from decimal import Decimal, InvalidOperation
import re

TASK_TYPES = ("read", "extract", "edit", "code", "debug", "architecture", "reason", "general")
BASE = {"read": 3.2, "extract": 3.4, "edit": 3.5, "code": 5.1,
        "debug": 6.0, "architecture": 7.5, "reason": 6.3, "general": 4.0}
# Quoted targets and code spans are objects, not evidence that the requested
# operation needs architectural reasoning. Only action-bearing text is scored.
QUOTED = re.compile(r'`[^`]*`|"[^"\n]*"|\'[^\'\n]*\'|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』')
ACTIONS = (
    ("reason", r'\b(?:prove|disprove|derive|deduce|reason|analyze|counterexample)\b|证明|证伪|推导|推理|论证|反例'),
    ("debug", r'\b(?:debug|diagnose|troubleshoot|trace)\b|\b(?:repair|fix)\b.{0,60}\b(?:class|function|method|cache|bug|error|failure)\b|排查|调试|定位.*(?:错误|故障)|诊断|修复.{0,20}(?:类|函数|错误|故障)'),
    ("architecture", r'\b(?:architect|design|redesign)\b.{0,60}\b(?:system|architecture|platform)\b|(?:设计|重建|规划|重构).{0,20}(?:架构|系统|平台)'),
    ("code", r'\b(?:implement|refactor|program|develop)\b|编程|实现|重构|开发'),
    ("edit", r'\b(?:edit|replace|rename|reword|polish|rewrite|change|changing|modify|modifying|repair|fix)\b|修改|编辑|润色|替换|改为|改成|重写|改名'),
    ("extract", r'\b(?:extract|collect|parse)\b|提取|抽取|采集'),
    ("read", r'\b(?:read|summarize|inspect|lookup|review)\b|阅读|总结|查阅|浏览'),
)
UNCERTAIN = r'\b(?:unknown|unclear|ambiguous|uncertain)\b|不确定|未知|不清楚|模糊'


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
    """Expose categorical features only. Confidence labels are uncalibrated."""
    if task_type not in TASK_TYPES or phase not in ("think", "act", "review"):
        raise ValueError("invalid task type or phase")
    if complexity not in ("simple", "normal", "hard") or risk not in ("low", "medium", "high"):
        raise ValueError("invalid complexity or risk")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("failures must be a nonnegative integer")
    if source not in ("heuristic", "caller_assessment"):
        raise ValueError("invalid score source")
    action_text = QUOTED.sub(" TARGET ", text.lower())
    found = [kind for kind, pattern in ACTIONS if re.search(pattern, action_text, re.S)]
    kind = task_type if task_type != "general" else (found[0] if found else "general")
    reasons = [f"task_type={kind}"]
    if task_type != "general":
        reasons.append("explicit_task_type")
    features = {"actions": found, "scope": "unspecified", "constraints": []}
    lexical = kind == "edit" and bool(re.search(r'\b(?:replace|rename)\b|替换|改名', action_text))
    bounded = bool(re.search(r'\b(?:only|single|one|word|line)\b|只|仅|一个|一处|词|这一行', action_text))
    proof = kind == "reason" and bool(re.search(r'\b(?:theorem|lemma|proof)\b|定理|引理|证明', action_text))
    adversarial = kind == "reason" and bool(re.search(r'\b(?:counterexample|disprove)\b|反例|证伪', action_text))
    if lexical or bounded:
        features["scope"] = "local" if kind == "edit" else "bounded"
    if lexical:
        features["constraints"].append("lexical_change")
    if proof:
        features["constraints"].append("proof_obligation")
    if adversarial:
        features["constraints"].append("adversarial_check")
    if re.search(r'\bpreserve\b.{0,50}\b(?:other|every|byte|content)\b|保留.{0,15}(?:其他|其余|每个|内容)|其他.*不变', action_text):
        features["constraints"].append("preserve_non_target_content")
    if score is None:
        value = BASE[kind]
        if lexical and bounded:
            value = 2.0
            reasons.append("bounded_lexical_edit")
        value += {"simple": -0.8, "normal": 0, "hard": 1.4}[complexity]
        value += {"low": 0, "medium": 0.7, "high": 1.7}[risk]
        value += min(failures, 2) * 0.6
        if proof and adversarial:
            value += 1.2
            reasons.append("proof_and_counterexample")
        if re.search(UNCERTAIN, action_text):
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
        src = "heuristic"
    else:
        value = _score(score)
        reasons.append("explicit_score")
        src = "caller_assessment" if source == "heuristic" else source
    return {"score": value, "source": src,
            "confidence": "medium" if kind != "general" or score is not None else "low",
            "confidence_calibrated": False, "task_type": kind,
            "features": features, "reasons": reasons}
