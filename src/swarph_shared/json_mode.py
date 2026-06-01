"""JSON-mode parsing harness for vendor LLMs that drift from strict-JSON output.

Two surfaces:

  - ``parse_json(text)`` — best-effort parse with prose-extraction fallback
    (claude / gemini / etc. sometimes wrap JSON in prose or markdown fences
    even when instructed not to). Returns ``(parsed_dict_or_None, error_str)``.

  - ``parse_json_with_retry(text, on_retry)`` — same, but on parse failure
    invokes the ``on_retry`` callback ONCE with structured retry feedback.
    The callback is responsible for re-calling the LLM with the feedback
    appended as a NEW ``[USER]`` turn (not concatenated to the prompt) — this
    multi-turn-preserving shape is the canonical retry pattern, locked in
    via lab DM #596 review of opus_subscription PR #125.

  - ``build_retry_feedback_turn(error)`` — helper for callers building their
    own retry logic that don't use the harness directly.

**Why this lives in swarph-shared, not in each adapter**: every vendor adapter
that wants JSON-mode would otherwise re-implement parsing + retry. Some would
forget the [USER]-turn pattern (concatenate the feedback instead). The lab+drop
DM thread surfaced this exact bug class as a "vendor-drift trap" — the harness
locks the right shape at the substrate so future-Claude can't re-derive it
wrong.

Reference:
  - opus_subscription PR #125 nit: retry feedback as new [USER] turn, not
    concatenated to the prompt. Multi-turn semantics matter.
  - lab DM #596 review: explicit ratification of the [USER]-turn shape
"""

from __future__ import annotations

import json
from typing import Callable, Optional


def parse_json(text: str) -> tuple[Optional[dict], Optional[str]]:
    """Best-effort JSON parse with prose-extraction fallback.

    Strategy:
      1. ``json.loads(text)`` directly
      2. On failure, find first ``{``, walk to balanced ``}``, parse that span
      3. On second failure, return ``(None, error_str)``

    Handles real LLM output shapes:

      - Pure JSON: ``{"a": 1}``
      - Prose-wrapped: ``Sure! Here's the JSON: {"a": 1} Let me know.``
      - Markdown-fenced: ```` ```json\\n{"a": 1}\\n``` ```` (extracted by step 2)

    Returns:
        ``(parsed_dict, None)`` on success.
        ``(None, error_str)`` on failure — error is one of "empty response",
        "no JSON object in response", "unbalanced braces", or the underlying
        ``json.JSONDecodeError`` message.
    """
    text = (text or "").strip()
    if not text:
        return None, "empty response"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    # Try to extract the first JSON object embedded in prose. Use the stdlib
    # scanner (raw_decode) from the first ``{`` rather than a hand-rolled
    # brace counter: raw_decode correctly tracks string literals + escapes, so
    # a ``{`` or ``}`` INSIDE a string value (code snippets, regex, prose with
    # braces) no longer truncates an otherwise-valid object. The old depth
    # counter closed at the first in-string ``}`` and sliced invalid JSON.
    start = text.find("{")
    if start < 0:
        return None, "no JSON object in response"
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as e2:
        return None, str(e2)
    if not isinstance(obj, dict):
        return None, "no JSON object in response"
    return obj, None


def build_retry_feedback_turn(error: str) -> str:
    """Build the canonical retry feedback content for a NEW [USER] turn.

    Use this in callers that build their own retry logic (don't use the
    ``parse_json_with_retry`` harness directly). The string returned here is
    the CONTENT of a new ``[USER]`` turn — the caller appends it to their
    messages list, not to their prompt string.

    This is the multi-turn-preserving pattern from PR #125 nit: feedback as
    a new conversational turn, NOT a concatenation onto the previous prompt.
    Concatenation drifts model context (model thinks the feedback is part of
    the original user message); new turn preserves the actual conversation
    shape.

    Args:
        error: parse error string (from ``parse_json`` second return value).

    Returns:
        Content string for a new ``[USER]`` turn instructing the model to
        emit valid JSON only.
    """
    return (
        "Your previous response was not valid JSON. "
        f"Error: {error}. "
        "Output ONLY valid JSON matching the schema. "
        "No prose, no markdown fences."
    )


def parse_json_with_retry(
    text: str,
    *,
    on_retry: Callable[[str], str],
) -> tuple[Optional[dict], Optional[str]]:
    """Parse text as JSON. On failure, call ``on_retry(retry_feedback)`` once.

    The ``on_retry`` callback is the seam where the caller plugs in their LLM
    invocation. The callback receives the retry feedback content (built via
    ``build_retry_feedback_turn``) and must:

      1. Append it as a NEW [USER] turn to the existing message history
      2. Re-call the LLM
      3. Return the new response text

    The harness then re-parses that new text. Returns ``(parsed_or_None,
    error_class_or_None)`` where error_class is one of:

      - ``None`` — parse succeeded
      - ``"malformed_json"`` — both initial parse AND retry parse failed
      - ``"retry_failed"`` — on_retry callback raised; underlying error in
        message but no second parse attempted

    Example::

        def on_retry(feedback: str) -> str:
            messages.append({"role": "user", "content": feedback})
            response = llm.invoke(messages)
            return response.text

        parsed, err = parse_json_with_retry(initial_text, on_retry=on_retry)

    Args:
        text: initial LLM response text to parse.
        on_retry: callable taking retry-feedback string, returning new
                  response text. Caller's responsibility to preserve message
                  history + invoke the LLM.

    Returns:
        ``(parsed_dict, None)`` on success.
        ``(None, "malformed_json")`` if both parse attempts fail.
        ``(None, "retry_failed")`` if on_retry callback raises.
    """
    parsed, err = parse_json(text)
    if parsed is not None:
        return parsed, None

    feedback = build_retry_feedback_turn(err or "parse failed")
    try:
        retry_text = on_retry(feedback)
    except Exception:
        return None, "retry_failed"

    parsed_retry, _ = parse_json(retry_text)
    if parsed_retry is not None:
        return parsed_retry, None
    return None, "malformed_json"
