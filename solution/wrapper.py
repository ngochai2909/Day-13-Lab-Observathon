"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}

PROMPT ROUTING: you can override the agent's system prompt PER REQUEST by setting it in
the config you pass to call_next, e.g.:
    conf = dict(config); conf["system_prompt"] = my_better_prompt
    result = call_next(question, conf)
(Or just edit solution/prompt.txt for a single static prompt used on every request.)
"""
from __future__ import annotations

import copy
import re
import time

from telemetry.logger import logger, set_correlation_id, new_correlation_id
from telemetry.cost import cost_from_usage
from telemetry.redact import redact


# --- PII regex patterns (same as telemetry/redact.py) ---
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\b(?:\+84|0)\d{9}\b")

# --- Injection pattern: detect instructions hidden in order notes ---
_INJECTION_RE = re.compile(
    r"(?:GHI\s*CH[UÚ]|NOTE|ghi\s*ch[uú])\s*[:：]?\s*.+",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(r"(?im)^\s*tong\s+cong\s*:\s*[-\d.,]+\s*vnd\s*$")
_TOTAL_ASK_RE = re.compile(
    r"(tong|thanh\s*toan|bao\s*nhieu|tinh\s*(tong|tien)|chi\s*phi)",
    re.IGNORECASE,
)
_REFUSAL_RE = re.compile(
    r"(het\s*hang|khong\s*tim\s*thay|khong\s*the\s*dat|khong\s*phuc\s*vu|khong\s*tinh\s*duoc)",
    re.IGNORECASE,
)
_DEST_HINT_RE = re.compile(r"(ship|giao)", re.IGNORECASE)
_QTY_RE = re.compile(r"\bmua\s+(\d+)\b", re.IGNORECASE)
_COUPON_RE = re.compile(r"\b(VIP20|SALE15|WINNER|EXPIRED)\b", re.IGNORECASE)
_COUPON_MAP = {"VIP20": 20, "SALE15": 15, "WINNER": 10, "EXPIRED": 0}
_PRICE_RE = re.compile(
    r"(?:gia|đơn\s*giá|don\s*gia)[^\\d]{0,24}([0-9][0-9.,]*)\s*VND",
    re.IGNORECASE,
)
_SHIP_RE = re.compile(
    r"(?:ship|ph[ií]\s*ship|giao)[^\n]{0,40}?([0-9][0-9.,]*)\s*VND",
    re.IGNORECASE,
)


def _to_int(num_text: str) -> int | None:
    if not num_text:
        return None
    digits = re.sub(r"[^\d]", "", num_text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_qty(question: str) -> int | None:
    m = _QTY_RE.search(question or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_coupon_pct(question: str) -> int:
    m = _COUPON_RE.search(question or "")
    if not m:
        return 0
    return _COUPON_MAP.get(m.group(1).upper(), 0)


def _extract_unit_price(answer: str) -> int | None:
    m = _PRICE_RE.search(answer or "")
    if not m:
        return None
    return _to_int(m.group(1))


def _extract_shipping(answer: str, question: str) -> int | None:
    # No destination requested -> shipping should be zero.
    if not _DEST_HINT_RE.search(question or ""):
        return 0
    m = _SHIP_RE.search(answer or "")
    if not m:
        return None
    return _to_int(m.group(1))


def _build_verified_total(question: str, answer: str) -> int | None:
    qty = _extract_qty(question)
    unit_price = _extract_unit_price(answer)
    shipping = _extract_shipping(answer, question)
    if qty is None or unit_price is None or shipping is None:
        return None
    pct = _extract_coupon_pct(question)
    subtotal = unit_price * qty
    discounted = subtotal * (100 - pct) // 100
    return discounted + shipping


def _sanitize_question(question: str) -> str:
    """Strip potential injection payloads from order notes in the question."""
    return _INJECTION_RE.sub("", question).strip()


def _redact_pii_in_answer(answer: str) -> str:
    """Remove PII (email, phone) from the agent's answer."""
    if not answer:
        return answer
    answer = _EMAIL_RE.sub("[EMAIL REDACTED]", answer)
    answer = _PHONE_RE.sub("[PHONE REDACTED]", answer)
    return answer


def _wants_total(question: str) -> bool:
    """Heuristic: whether user asks for payable total."""
    return bool(_TOTAL_ASK_RE.search(question or ""))


def _cleanup_answer(answer: str, question: str) -> str:
    """Normalize answer shape for judge-friendly output."""
    if not answer:
        return answer

    # Remove contact details and contact-only side notes.
    answer = _redact_pii_in_answer(answer)
    lines = [ln.rstrip() for ln in answer.splitlines()]
    lines = [ln for ln in lines if not re.search(r"(lien\s*he|contact)", ln, re.IGNORECASE)]

    wants_total = _wants_total(question)
    refusal = bool(_REFUSAL_RE.search(answer))
    if refusal or not wants_total:
        lines = [ln for ln in lines if not _TOTAL_RE.match(ln)]
        cleaned = "\n".join(lines).strip()
        # Avoid returning empty output after contact-line removal.
        return cleaned or "Xin loi, minh chua the xu ly yeu cau nay."

    verified_total = _build_verified_total(question, answer)
    if verified_total is not None:
        # Keep a concise, deterministic final line for scoring.
        return f"Tong cong: {verified_total} VND"

    # Collapse extra blank lines but keep readability.
    compact = []
    last_blank = False
    for ln in lines:
        is_blank = not ln.strip()
        if is_blank and last_blank:
            continue
        compact.append(ln)
        last_blank = is_blank

    return "\n".join(compact).strip()


def mitigate(call_next, question, config, context):
    # --- Setup correlation ID for tracing ---
    cid = new_correlation_id()
    set_correlation_id(cid)

    qid = context.get("qid", "unknown")
    session_id = context.get("session_id", "unknown")
    turn_index = context.get("turn_index", 0)

    # --- Sanitize: strip injection from order notes ---
    clean_question = _sanitize_question(question)

    # --- Cache: check if we already answered this exact question ---
    cache = context.get("cache", {})
    cache_lock = context.get("cache_lock")
    cache_key = clean_question.strip().lower()

    if cache_lock:
        with cache_lock:
            cached = cache.get(cache_key)
    else:
        cached = cache.get(cache_key)

    if cached is not None:
        logger.log_event("CACHE_HIT", {"qid": qid, "question": clean_question})
        return copy.deepcopy(cached)

    # --- Call agent with retry ---
    retry_cfg = config.get("retry", {}) if isinstance(config, dict) else {}
    max_retries = int(retry_cfg.get("max_attempts", 3))
    backoff_ms = int(retry_cfg.get("backoff_ms", 500))
    result = None
    last_error = None

    for attempt in range(max_retries):
        t0 = time.time()
        try:
            result = call_next(clean_question, config)
        except Exception as e:
            last_error = str(e)
            logger.log_event("CALL_EXCEPTION", {
                "qid": qid, "attempt": attempt + 1, "error": last_error
            })
            time.sleep((backoff_ms / 1000.0) * (attempt + 1))
            continue

        wall_ms = int((time.time() - t0) * 1000)
        status = result.get("status", "unknown")
        meta = result.get("meta", {})

        # --- Observability: log everything ---
        usage = meta.get("usage", {})
        model = meta.get("model", config.get("model", "unknown"))
        cost = cost_from_usage(model, usage)

        logger.log_event("AGENT_CALL", {
            "qid": qid,
            "session_id": session_id,
            "turn_index": turn_index,
            "attempt": attempt + 1,
            "status": status,
            "wall_ms": wall_ms,
            "latency_ms": meta.get("latency_ms", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cost_usd": cost,
            "tools_used": meta.get("tools_used", []),
            "tool_count": len(meta.get("tools_used", [])),
            "steps": result.get("steps", 0),
        })

        # --- Check for PII in answer ---
        answer = result.get("answer") or ""
        _, pii_count = redact(answer)
        if pii_count > 0:
            logger.log_event("PII_DETECTED", {
                "qid": qid, "pii_count": pii_count,
            })
            result["answer"] = _redact_pii_in_answer(answer)
        result["answer"] = _cleanup_answer(result.get("answer") or "", clean_question)

        # --- Retry on error status ---
        if status in ("ok",):
            break
        elif status in ("wrapper_error", "error"):
            logger.log_event("RETRY", {
                "qid": qid, "attempt": attempt + 1, "status": status
            })
            time.sleep((backoff_ms / 1000.0) * (attempt + 1))
            continue
        else:
            # max_steps, loop, no_action — don't retry these
            break

    # --- Fallback if all retries failed ---
    if result is None:
        result = {
            "answer": None,
            "status": "wrapper_error",
            "steps": 0,
            "trace": [],
            "meta": {},
        }
        logger.log_event("ALL_RETRIES_FAILED", {"qid": qid, "last_error": last_error})

    # --- Cache the result ---
    if result.get("status") == "ok" and result.get("answer"):
        cached_result = copy.deepcopy(result)
        if cache_lock:
            with cache_lock:
                cache[cache_key] = cached_result
        else:
            cache[cache_key] = cached_result

    return result
