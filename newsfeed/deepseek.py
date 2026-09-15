"""The one HTTPS client for every DeepSeek call, with the retry rules from section 7.6.

Raw urllib, no SDK, like the rest of the repo. Every model call in the pipeline goes through here,
so the retry, spend and failure rules live in one place and one place only.

What each answer does (docs/ARCHITECTURE.md section 7.6):

| Answer                                   | Action                                              |
|------------------------------------------|-----------------------------------------------------|
| 200 with content                         | Accept                                              |
| 200 with empty content                   | Back off and retry, up to 3 times                   |
| finish_reason: length                    | Retry once with max_tokens doubled                  |
| 429, 5xx, timeout, connection error      | Exponential backoff with jitter, up to 5 tries      |
| 402                                      | BalanceError: stop every model stage and alert      |
| 400, 401, 422                            | FatalError: the request or the key is wrong         |

A TransientError means the work is left for the next run. A FatalError means the stage stops. The
caller's one schema retry is separate and is never used up by anything here.
"""

from __future__ import annotations

import json
import random as random_module
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

API_ROOT = "https://api.deepseek.com"
CHAT_URL = f"{API_ROOT}/chat/completions"
MODELS_URL = f"{API_ROOT}/models"

DEFAULT_MODEL = "deepseek-flash"
VERIFY_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT = 120

# Tries for the 429, 5xx, timeout and connection-error class, and for empty 200s.
MAX_TRANSPORT_ATTEMPTS = 5
MAX_EMPTY_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
FATAL_STATUSES = frozenset({400, 401, 403, 404, 422})


@dataclass(frozen=True)
class Prices:
    """USD per 1,000,000 tokens, at the peak rate."""

    cache_hit: float
    cache_miss: float
    output: float


# Peak rates from https://api-docs.deepseek.com/quick_start/pricing, read on 2026-09-15.
# deepseek-flash's rates took effect at 04:00 UTC on 2026-09-10. Off-peak is half of these.
# These numbers are part of what the cost column means, so re-read the page whenever the model or
# the prompts change, and record the change beside the config hash (section 10.5).
PRICES: dict[str, Prices] = {
    "deepseek-flash": Prices(cache_hit=0.006, cache_miss=0.30, output=1.20),
    "deepseek-v4-pro": Prices(cache_hit=0.044, cache_miss=1.32, output=3.96),
}
# UTC hours, Monday to Friday, charged at the peak rate. 01:00 to 04:00 and 06:00 to 10:00.
PEAK_HOURS = frozenset(range(1, 4)) | frozenset(range(6, 10))


def is_peak(when: datetime | None = None) -> bool:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.weekday() < 5 and when.hour in PEAK_HOURS


def prices_for(model: str) -> Prices:
    """The rates for a model, or the dearest rates known when the model is not in the table.

    Guessing high is deliberate. These numbers feed the daily spend cap, and a model id this build
    has never seen must not be able to spend without limit because it priced as free.
    """
    known = PRICES.get(model)
    if known is not None:
        return known
    dearest = max(PRICES.values(), key=lambda p: p.output)
    return dearest


@dataclass(frozen=True)
class Usage:
    """Tokens for one call. The cache fields are what make a repeated system prompt cheap."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> Usage:
        usage = payload if isinstance(payload, dict) else {}

        def number(*names: str) -> int:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int):
                    return value
            return 0

        prompt = number("prompt_tokens")
        hit = number("prompt_cache_hit_tokens")
        miss = number("prompt_cache_miss_tokens")
        # Older answers send only prompt_tokens. Treat all of it as a miss, which prices it high.
        if hit == 0 and miss == 0:
            miss = prompt
        return cls(
            prompt_tokens=prompt,
            completion_tokens=number("completion_tokens"),
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
        )


def cost_usd(model: str, usage: Usage, when: datetime | None = None) -> float:
    """What one call cost, in USD, at the rate in force when it was made."""
    prices = prices_for(model)
    rate = 1.0 if is_peak(when) else 0.5
    millions = 1_000_000
    return rate * (
        usage.cache_hit_tokens * prices.cache_hit / millions
        + usage.cache_miss_tokens * prices.cache_miss / millions
        + usage.completion_tokens * prices.output / millions
    )


@dataclass(frozen=True)
class Completion:
    """One accepted answer, with everything the llm_calls row needs."""

    content: str
    model: str
    finish_reason: str
    usage: Usage
    cost_usd: float
    latency_ms: int
    attempts: int

    @property
    def truncated(self) -> bool:
        """The answer hit max_tokens even after the one doubling, so its JSON may be cut off."""
        return self.finish_reason == "length"


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TransientError(DeepSeekError):
    """Retries ran out. Leave this work for the next run; nothing is wrong with the request."""


class FatalError(DeepSeekError):
    """The request or the key is wrong. Stop the stage and alert; retrying cannot help."""


class BalanceError(FatalError):
    """402. The account is out of credit, so every model stage stops."""


# A transport takes (url, headers, body) and returns (status, headers, body). Injected in tests so
# the retry rules can be checked against recorded answers without a network.
Transport = Callable[[str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def urllib_transport(url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        # The body of an error carries DeepSeek's reason, so read it rather than discard it.
        return error.code, dict(error.headers or {}), error.read()


class Client:
    """One DeepSeek account. Safe to build per stage; it holds no connection."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        url: str = CHAT_URL,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random_module.random,
        max_transport_attempts: int = MAX_TRANSPORT_ATTEMPTS,
        max_empty_retries: int = MAX_EMPTY_RETRIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key:
            raise FatalError("DeepSeek was asked for a call with no API key.")
        self.api_key = api_key
        self.model = model
        self.url = url
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.jitter = jitter
        self.max_transport_attempts = max_transport_attempts
        self.max_empty_retries = max_empty_retries
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def build_payload(
        self, system: str, user: str, max_tokens: int, temperature: float, json_object: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def backoff(self, attempt: int, retry_after: str | None = None) -> float:
        """Exponential with jitter, honouring Retry-After when the answer sent one."""
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), BACKOFF_CAP_SECONDS))
            except ValueError:
                pass
        step = min(BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)), BACKOFF_CAP_SECONDS)
        return step * (0.5 + self.jitter())

    def models(self) -> list[str]:
        """Model ids the account can use. Section 7.6 says to confirm the id when M5 starts."""
        status, _, body = self.transport(MODELS_URL, self.headers(), None)
        if status != 200:
            raise FatalError(f"DeepSeek answered {status} to a models request.", status)
        payload = json.loads(body.decode("utf-8", "replace"))
        return [entry["id"] for entry in payload.get("data") or [] if isinstance(entry, dict) and entry.get("id")]

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
        json_object: bool = True,
    ) -> Completion:
        """One accepted answer, or an error saying whether to stop or to try again next run."""
        started = time.monotonic()
        attempts = 0
        transport_attempts = 0
        empty_answers = 0
        tokens = max_tokens
        doubled_once = False

        while True:
            attempts += 1
            payload = self.build_payload(system, user, tokens, temperature, json_object)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                status, headers, raw = self.transport(self.url, self.headers(), body)
            except (TimeoutError, OSError) as error:
                transport_attempts += 1
                if transport_attempts >= self.max_transport_attempts:
                    raise TransientError(
                        f"DeepSeek was unreachable after {transport_attempts} tries: {error}"
                    ) from error
                self.sleep(self.backoff(transport_attempts))
                continue

            if status == 402:
                raise BalanceError(f"DeepSeek answered 402: {_reason(raw)}", status)
            if status in FATAL_STATUSES:
                raise FatalError(f"DeepSeek answered {status}: {_reason(raw)}", status)
            if status in RETRYABLE_STATUSES:
                transport_attempts += 1
                if transport_attempts >= self.max_transport_attempts:
                    raise TransientError(
                        f"DeepSeek answered {status} on all {transport_attempts} tries: {_reason(raw)}",
                        status,
                    )
                self.sleep(self.backoff(transport_attempts, headers.get("Retry-After")))
                continue
            if status != 200:
                raise FatalError(f"DeepSeek answered an unexpected {status}: {_reason(raw)}", status)

            try:
                answer = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError as error:
                # A gateway can answer 200 with a page rather than JSON.
                transport_attempts += 1
                if transport_attempts >= self.max_transport_attempts:
                    raise TransientError("DeepSeek answered 200 with something that is not JSON.") from error
                self.sleep(self.backoff(transport_attempts))
                continue

            choices = answer.get("choices") or [{}]
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason") or ""

            # An answer cut off at max_tokens gets one doubling. If it is cut off again, hand it
            # back anyway: the caller's schema check rejects it and stores the reason, and a third
            # try would cost more and end the same way.
            if finish_reason == "length" and not doubled_once:
                doubled_once = True
                tokens *= 2
                continue

            if not content.strip():
                empty_answers += 1
                if empty_answers > self.max_empty_retries:
                    raise TransientError(
                        f"DeepSeek answered 200 with empty content {empty_answers} times."
                    )
                self.sleep(self.backoff(empty_answers))
                continue

            # The model string the answer carries, not the one asked for: section 10.5 puts it in
            # the config hash, and DeepSeek can serve a point release under the same alias.
            served = answer.get("model") or self.model
            usage = Usage.from_payload(answer.get("usage"))
            return Completion(
                content=content,
                model=served,
                finish_reason=finish_reason,
                usage=usage,
                cost_usd=cost_usd(served, usage, self.clock()),
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )


def _reason(raw: bytes) -> str:
    """DeepSeek's own message for an error, trimmed, or the raw body when it is not JSON."""
    text = raw.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return text[:200]
