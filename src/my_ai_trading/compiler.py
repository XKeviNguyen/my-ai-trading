"""Natural-language → typed rule proposal (PRD F05). Deterministic, offline, zero tokens.

The output is a *proposal*: unsupported fragments are listed, assumptions must be confirmed, and
nothing is activated here. Prompts are untrusted data; they can only ever become a validated
``StrategyRule``. An optional LLM proposer must feed the same validator (see ``from_llm_json``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .domain import Timeframe
from .rules import AllOf, AnyOf, Compare, MetricRef, StrategyRule, Trend, _node_tfs, _walk, preview

MAX_PROMPT_CHARS = 2_000
MAX_PROMPTS = 10
MAX_CLAUSE_CHARS = 200  # with whitespace collapsed, this bounds regex work per clause

_UNSAFE = [
    (r"\b(python|eval|exec|import\s+\w+|subprocess|os\.system|shell|bash|sudo|rm\s+-rf|sql|drop\s+table)\b",
     "code or command execution is never allowed; rules are data only"),
    (r"\b(api[\s_-]?keys?|secret|password|private[\s_-]?key|seed phrase|mnemonic|(?:access|auth|bearer) tokens?)\b",
     "credentials must never be requested or placed in a strategy"),
    (r"\b(place|submit|send|execute)\s+(an?\s+)?(market\s+|limit\s+)?(order|trade)s?\b|\bwithdraw|\bbuy for me\b|\bsell for me\b",
     "the platform is read-only and never places orders"),
    (r"ignore (all |any )?(previous|prior|above) (instructions|rules)|system prompt|developer mode|jailbreak",
     "attempts to override system controls are rejected"),
]

_TF_WORDS = {
    "hourly": "1h", "1-hour": "1h", "one-hour": "1h", "4-hour": "4h", "four-hour": "4h", "daily": "1d",
    "day": "1d", "weekly": "1w", "week": "1w", "15-minute": "15m", "5-minute": "5m", "30-minute": "30m",
    "2-hour": "2h", "12-hour": "12h", "monthly": "1M", "month": "1M",
}
_TF = r"(?P<tf>\d{1,2}\s?[mhdw]|hourly|daily|weekly|monthly|\d{1,2}-(?:minute|hour)|one-hour|four-hour|day|week|month)"
_MA = r"(?P<ma>sma|ema|ma)\s?\(?(?P<n>\d{1,3})\)?"


def _tf(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.lower().replace(" ", "")
    if raw in _TF_WORDS:
        return _TF_WORDS[raw]
    m = re.fullmatch(r"(\d{1,2})([mhdw])", raw)
    if m:
        code = f"{int(m.group(1))}{m.group(2)}"
        return code if code in {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"} else None
    return None


@dataclass
class Proposal:
    status: str                       # COMPILED | NEEDS_CONFIRMATION | UNRESOLVED | REJECTED
    rule: StrategyRule | None = None
    assumptions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)
    clauses: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "assumptions": self.assumptions, "unresolved": self.unresolved,
                "rejected_reasons": self.rejected_reasons, "clauses": self.clauses,
                "rule": self.rule.model_dump(mode="json") if self.rule else None,
                "preview": preview(self.rule) if self.rule else None,
                "activation": "requires explicit user approval of this exact rule_hash" if self.rule else
                              "not activatable"}


def _metric(ma: str, n: str, tf: str) -> MetricRef:
    name = "ema" if ma.lower() == "ema" else "sma"
    return MetricRef(name=name, period=int(n), timeframe=tf)


def _parse_clause(text: str, default_tf: str | None, assumptions: list[str],
                  context: dict[str, str]) -> Any | None:
    # "1M" means one month; lowercasing would turn it into "1m" (one minute).
    t = re.sub(r"\s+", " ", re.sub(r"\b1M\b", "monthly", text)).strip().lower()  # single spaces: no backtracking over whitespace runs
    if len(t) > MAX_CLAUSE_CHARS:
        return None

    def need_tf(found: str | None, what: str) -> str | None:
        tf = _tf(found)
        if tf:
            context["tf"] = tf
            return tf
        if context.get("tf"):
            assumptions.append(f"'{what}': no timeframe given; assumed {context['tf']} from the earlier clause")
            return context["tf"]
        if default_tf:
            assumptions.append(f"'{what}': no timeframe given; assumed chart timeframe {default_tf}")
            return default_tf
        return None

    m = re.search(rf"(?:(?:an?|the) )?(?:{_TF} )?(?:(?:candle|bar|close|price) )?(?:closes?|is|trades?) "
                  rf"(?P<dir>above|below|over|under)\s+(?:the\s+)?{_MA}(?:\s+on\s+(?:the\s+)?{_TF.replace('tf', 'tf2')})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf2"), text)
        if not tf:
            return None
        op = ">" if m.group("dir") in ("above", "over") else "<"
        if m.group("ma") == "ma":
            assumptions.append(f"'MA{m.group('n')}' interpreted as a simple moving average (SMA)")
        return Compare(left=MetricRef(name="close", timeframe=tf), op=op, right=_metric(m.group("ma"), m.group("n"), tf))

    m = re.search(rf"(?:{_TF} )?(?:(?:price|close) )?cross(?:es)? (?P<dir>above|below)\s+(?:the\s+)?{_MA}", t)
    if m:
        tf = need_tf(m.group("tf"), text)
        if not tf:
            return None
        return Compare(left=MetricRef(name="close", timeframe=tf),
                       op="crosses_above" if m.group("dir") == "above" else "crosses_below",
                       right=_metric(m.group("ma"), m.group("n"), tf))

    m = re.search(r"volume\s+(?:is\s+)?(?:at least|>=|above|over|greater than|more than)\s+(?P<k>\d+(?:\.\d+)?)\s?x"
                  r"(?:\s+(?:its|the))?\s+(?:prior|previous|last)\s+(?P<n>\d{1,3})[- ]?(?:bar|candle|period)s?"
                  rf"(?:\s+average)?(?:\s+on\s+(?:the\s+)?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf"), text)
        if not tf:
            return None
        if "more than" in t or "greater than" in t or "above" in t or "over" in t:
            op = ">"
        else:
            op = ">="
        return Compare(left=MetricRef(name="volume_ratio", period=int(m.group("n")), timeframe=tf),
                       op=op, right=float(m.group("k")))

    m = re.search(rf"(?:{_TF.replace('tf', 'tf0')} )?adx\s?\(?(?P<n>\d{{1,3}})?\)?\s+(?:is\s+)?(?P<dir>below|above|under|over)\s+(?P<v>\d{{1,3}}(?:\.\d+)?)"
                  rf"(?:\s+on\s+(?:the\s+)?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf0"), text)
        if not tf:
            return None
        if not m.group("n"):
            assumptions.append("ADX period not given; assumed 14 (Wilder)")
        return Compare(left=MetricRef(name="adx", period=int(m.group("n") or 14), timeframe=tf),
                       op="<" if m.group("dir") in ("below", "under") else ">", right=float(m.group("v")))

    m = re.search(rf"(?:{_TF.replace('tf', 'tf0')} )?macd(?: line)? cross(?:es)? (?P<dir>above|below) (?:the )?signal(?: line)?"
                  rf"(?: on (?:the )?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf0"), text)
        if not tf:
            return None
        assumptions.append("MACD uses the standard 12/26/9 periods")
        return Compare(left=MetricRef(name="macd_histogram", timeframe=tf),
                       op="crosses_above" if m.group("dir") == "above" else "crosses_below", right=0.0)

    m = re.search(rf"(?:{_TF.replace('tf', 'tf0')} )?macd(?: (?P<part>histogram|line))? (?:is )?(?P<dir>above|below|over|under) "
                  rf"(?:zero|0)(?: on (?:the )?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf0"), text)
        if not tf:
            return None
        assumptions.append("MACD uses the standard 12/26/9 periods")
        # Bare "MACD" is the MACD line; only an explicit "histogram" tests MACD minus signal.
        name = "macd_histogram" if m.group("part") == "histogram" else "macd_line"
        return Compare(left=MetricRef(name=name, timeframe=tf),
                       op=">" if m.group("dir") in ("above", "over") else "<", right=0.0)

    m = re.search(rf"(?:{_TF} )?(?:close|price) (?:closes |is )?(?P<dir>above|below) (?:the )?(?P<band>upper|lower) "
                  rf"bollinger(?: band)?(?: on (?:the )?{_TF.replace('tf', 'tf2')})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf2"), text)
        if not tf:
            return None
        assumptions.append("Bollinger Bands use 20 periods and 2 standard deviations")
        above = m.group("dir") == "above"
        level = 1.0 if m.group("band") == "upper" else 0.0
        return Compare(left=MetricRef(name="bollinger_percent_b", period=20, timeframe=tf),
                       op=">" if above else "<", right=level)

    m = re.search(rf"(?:{_TF.replace('tf', 'tf0')} )?rsi\s?\(?(?P<n>\d{{1,3}})?\)?\s+(?:is\s+)?(?P<dir>below|above|under|over)\s+(?P<v>\d{{1,3}}(?:\.\d+)?)"
                  rf"(?:\s+on\s+(?:the\s+)?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf") or m.group("tf0"), text)
        if not tf:
            return None
        n = m.group("n")
        if not n:
            assumptions.append("RSI period not given; assumed 14 (Wilder)")
        return Compare(left=MetricRef(name="rsi_wilder", period=int(n or 14), timeframe=tf),
                       op="<" if m.group("dir") in ("below", "under") else ">", right=float(m.group("v")))

    m = re.search(rf"(?:breaks?|closes?)\s+(?P<dir>above|below)\s+(?:the\s+)?(?:prior|previous)\s+(?P<n>\d{{1,3}})[- ]?"
                  rf"(?:bar|candle|day)s?\s+(?P<hl>high|low)(?:\s+on\s+(?:the\s+)?{_TF})?", t)
    if m:
        tf = need_tf(m.group("tf"), text)
        if not tf:
            return None
        name = "prior_high" if m.group("hl") == "high" else "prior_low"
        return Compare(left=MetricRef(name="close", timeframe=tf), op=">" if m.group("dir") == "above" else "<",
                       right=MetricRef(name=name, period=int(m.group("n")), timeframe=tf))

    m = re.search(rf"{_TF}\s+trend\s+(?:is\s+)?(?P<state>bullish|bearish|up|down|uptrend|downtrend)", t)
    if m:
        tf = _tf(m.group("tf"))
        if not tf:
            return None
        state = "UP" if m.group("state") in ("bullish", "up", "uptrend") else "DOWN"
        assumptions.append(f"'{m.group(0)}' is ambiguous; proposed definition: {tf} close > SMA50 > SMA200 with "
                           f"SMA50 rising (mirror for bearish). Confirm or edit.")
        return Trend(timeframe=tf, state=state)
    return None


_PREFIX = re.compile(r"^\s*(?:please\s+)?(?:alert me|notify me|tell me|signal)?\s*(?:only\s+)?(?:if|when)\s+", re.I)
_OTHERWISE = re.compile(r"[;,.]?\s*otherwise\s+(?:show\s+)?(?P<o>wait|nothing|no alert)\.?\s*$", re.I)


def compile_prompts(prompts: list[str], *, name: str = "Custom strategy",
                    default_timeframe: str | None = None) -> Proposal:
    if not prompts or len(prompts) > MAX_PROMPTS:
        return Proposal("REJECTED", rejected_reasons=[f"provide 1..{MAX_PROMPTS} prompts"])
    reasons = []
    for p in prompts:
        if not isinstance(p, str) or len(p) > MAX_PROMPT_CHARS:
            return Proposal("REJECTED", rejected_reasons=[f"each prompt must be text up to {MAX_PROMPT_CHARS} chars"])
        for pattern, why in _UNSAFE:
            if re.search(pattern, p, re.I):
                reasons.append(why)
    if reasons:
        return Proposal("REJECTED", rejected_reasons=sorted(set(reasons)))

    assumptions: list[str] = []
    unresolved: list[str] = []
    clauses: list[dict[str, str]] = []
    nodes: list[Any] = []
    otherwise = "WAIT"
    for p in prompts:
        text = re.sub(r"\s+", " ", p).strip()
        om = _OTHERWISE.search(text)
        if om:
            otherwise = "WAIT" if om.group("o").lower() == "wait" else "NO_ALERT"
            text = text[:om.start()]
        text = _PREFIX.sub("", text).strip().rstrip(".")
        text = re.sub(r"^(?:also|and)\s+(?:require\s+|only\s+if\s+)?", "", text, flags=re.I)
        has_and = re.search(r"\band\b|,", text, re.I)
        has_or = re.search(r"\bor\b", text, re.I)
        if has_and and has_or:
            unresolved.append(f"'{p}': mixes AND and OR without grouping; split it into separate prompts")
            continue
        parts = [s for s in re.split(r"\s*(?:,\s*and\s+|,\s*|\band\b|\bor\b)\s*", text, flags=re.I) if s.strip()]
        parsed = []
        context: dict[str, str] = {}
        for part in parts:
            node = _parse_clause(part, default_timeframe, assumptions, context)
            if node is None:
                unresolved.append(part.strip() if len(part) <= MAX_CLAUSE_CHARS
                                  else f"clause longer than {MAX_CLAUSE_CHARS} characters; split it up")
            else:
                parsed.append(node)
                clauses.append({"text": part.strip(), "kind": node.kind})
        if parsed:
            nodes.append(AnyOf(items=parsed) if has_or and len(parsed) > 1 else
                         parsed[0] if len(parsed) == 1 else AllOf(items=parsed))

    if not nodes:
        return Proposal("UNRESOLVED", assumptions=assumptions, unresolved=unresolved or ["nothing recognized"])
    condition = nodes[0] if len(nodes) == 1 else AllOf(items=nodes)
    trigger = _trigger_timeframe(condition)
    try:
        rule = StrategyRule(name=name, trigger_timeframe=trigger, condition=condition, otherwise=otherwise,
                            description=" | ".join(prompts)[:500])
    except ValidationError as exc:
        return Proposal("UNRESOLVED", unresolved=[f"compiled rule failed validation: {exc.errors()[0]['msg']}"])
    assumptions.append(f"Evaluated on each closed {trigger} bar (the smallest timeframe used in the rule)")
    status = "UNRESOLVED" if unresolved else "NEEDS_CONFIRMATION" if len(assumptions) > 1 else "COMPILED"
    return Proposal(status, rule, assumptions, unresolved, clauses=clauses)


def _trigger_timeframe(condition: Any) -> str:
    tfs: set[str] = set()
    _walk(condition, lambda n: tfs.update(_node_tfs(n)))
    return min(tfs, key=lambda t: Timeframe(t).duration_ms)


def from_llm_json(payload: Any) -> Proposal:
    """Validate an LLM-proposed rule exactly like any other untrusted input."""
    try:
        rule = StrategyRule.model_validate(payload)
    except ValidationError as exc:
        return Proposal("REJECTED", rejected_reasons=[f"invalid rule: {e['msg']}" for e in exc.errors()[:5]])
    return Proposal("NEEDS_CONFIRMATION", rule, assumptions=["Proposed by a language model; review every condition"])
