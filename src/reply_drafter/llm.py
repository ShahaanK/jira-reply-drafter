"""LLM access behind one function, so callers never touch the SDK directly.

Uses the Anthropic API with the API key (ANTHROPIC_API_KEY). Optional PII
pseudonymization before each call: SCRUB_PII_BEFORE_LLM (off by default).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from reply_drafter.config import Settings
from reply_drafter.scrub import Scrubber

log = logging.getLogger(__name__)

_SCRUB_NOTE = (
    "\n\nSome personal details in the input were replaced with placeholders such as "
    "[PERSON_1] or [EMAIL_1]. Keep any placeholder exactly as written if you need to "
    "refer to that detail; never guess the real value."
)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    scrubbed: bool
    redactions: dict[str, int] = field(default_factory=dict)
    sent_prompt: str = ""   # exactly what was sent (for the review page)


def generate(
    settings: Settings,
    *,
    system: str,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 1024,
    scrubber: Scrubber | None = None,
) -> LLMResult:
    model = model or settings.draft_model
    scrub = settings.scrub_pii_before_llm
    if scrub:
        scrubber = scrubber or Scrubber()
        system = scrubber.scrub(system) + _SCRUB_NOTE
        prompt = scrubber.scrub(prompt)

    text = _anthropic(settings, system, prompt, model, max_tokens)

    if scrub and scrubber is not None:
        text = scrubber.restore(text)
    return LLMResult(
        text=text.strip(), model=model, provider="anthropic", scrubbed=scrub,
        redactions=scrubber.counts if (scrub and scrubber) else {},
        sent_prompt=prompt,
    )


def _anthropic(settings: Settings, system: str, prompt: str, model: str, max_tokens: int) -> str:
    import anthropic

    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=settings.http_max_retries,
    )
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"Output truncated at max_tokens={max_tokens}")
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
