import os
import logging
import re
from typing import List, Dict, Optional, AsyncGenerator, Annotated
from contextlib import asynccontextmanager
import pendulum
import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, Response, StreamingResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
import json
import time
import backoff
import asyncio
import functools
import random

# Logging setup (early so config errors are visible)
log_level_name = os.getenv("UVICORN_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upstream-proxy")

# Configuration loading
PROXY_API_KEY = os.getenv("PROXY_API_KEY")
if not PROXY_API_KEY:
    logger.error("Error: Environment variable PROXY_API_KEY is not set.")
    exit(1)

UPSTREAM_BASE_URL = os.getenv(
    "UPSTREAM_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
IS_OPENROUTER = "openrouter.ai" in UPSTREAM_BASE_URL.lower()
# Official Anthropic Messages API uses x-api-key (+ anthropic-version), not Bearer.
IS_ANTHROPIC = "api.anthropic.com" in UPSTREAM_BASE_URL.lower()
TIMEZONE = os.getenv("TIMEZONE", "UTC")

try:
    DEFAULT_KEY_DAILY_LIMIT = int(
        os.getenv("DEFAULT_KEY_DAILY_LIMIT")
        or ("50" if IS_OPENROUTER else "125")
    )
except ValueError:
    DEFAULT_KEY_DAILY_LIMIT = 50 if IS_OPENROUTER else 125

try:
    KEY_MIN_INTERVAL_SECONDS = float(
        os.getenv("KEY_MIN_INTERVAL_SECONDS")
        or ("3.0" if IS_OPENROUTER else "0")
    )
except ValueError:
    KEY_MIN_INTERVAL_SECONDS = 3.0 if IS_OPENROUTER else 0.0


def parse_keys_config(
    config_str: str, default_limit: int = 50
) -> List[Dict]:
    """Parses the keys configuration string into a list of dictionaries"""
    keys = []
    for item in config_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            key, limit = item.split(":", 1)
            try:
                limit = int(limit)
            except ValueError:
                limit = default_limit
            keys.append({"key": key.strip(), "limit": limit})
        else:
            keys.append({"key": item, "limit": default_limit})
    return keys


# UPSTREAM_KEYS preferred; OPENROUTER_KEYS kept for backward compatibility
_keys_raw = os.getenv("UPSTREAM_KEYS") or os.getenv("OPENROUTER_KEYS", "")
UPSTREAM_CONFIG = parse_keys_config(_keys_raw, DEFAULT_KEY_DAILY_LIMIT)
UPSTREAM_KEYS = [k["key"] for k in UPSTREAM_CONFIG]
# Aliases so existing OpenRouter-oriented names keep working internally
OPENROUTER_CONFIG = UPSTREAM_CONFIG
OPENROUTER_KEYS = UPSTREAM_KEYS

if not UPSTREAM_KEYS:
    logger.error(
        "Error: Set UPSTREAM_KEYS (or OPENROUTER_KEYS) with at least one API key."
    )
    exit(1)

# Initialize key status
key_status: Dict[str, Optional[pendulum.DateTime]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes the application, API keys, and global HTTP client on startup"""
    global key_status, http_client
    if not UPSTREAM_KEYS:
        logger.error("No UPSTREAM_KEYS / OPENROUTER_KEYS provided! Exiting...")
        exit(1)

    key_status = {key: None for key in UPSTREAM_KEYS}
    # max_keepalive_connections: number of idle connections to keep open
    # max_connections: total number of concurrent connections
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    http_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(300.0))

    if IS_OPENROUTER:
        profile = "openrouter"
    elif IS_ANTHROPIC:
        profile = "anthropic"
    else:
        profile = "generic"
    logger.info(
        f"Initialized upstream={UPSTREAM_BASE_URL} profile={profile} "
        f"keys={len(UPSTREAM_KEYS)} default_daily_limit={DEFAULT_KEY_DAILY_LIMIT} "
        f"min_interval={KEY_MIN_INTERVAL_SECONDS}s timezone={TIMEZONE}"
    )

    yield

    """Closes the global HTTP client on application shutdown"""
    if http_client:
        await http_client.aclose()
        logger.info("Global HTTP client closed")


app = FastAPI(lifespan=lifespan)

http_client: Optional[httpx.AsyncClient] = None


def apply_upstream_key(headers: Dict[str, str], key: str) -> Dict[str, str]:
    """Stamp the selected upstream key into headers for the active protocol."""
    if IS_ANTHROPIC:
        headers["x-api-key"] = key
        headers.pop("Authorization", None)
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def non_streaming_retry():
    """Retry decorator for non-streaming requests (network errors only)"""
    return backoff.on_exception(
        backoff.expo,
        (httpx.RequestError, httpx.TimeoutException),
        max_tries=3,
    )


def async_retryable(func):
    """Decorator for retrying streaming requests with key switching logic.

    At most one attempt per upstream key for a single client request.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        last_exception = None
        attempts = 0
        max_attempts = len(UPSTREAM_KEYS)
        tried_keys = set()

        while attempts < max_attempts:
            attempts += 1
            selected_key = kwargs.get("selected_key")
            headers = kwargs.get("headers")
            if selected_key:
                tried_keys.add(selected_key)

            # Check if the key is blocked BEFORE making the request
            current_time = pendulum.now(TIMEZONE)
            lock_time = key_status.get(selected_key)
            is_active = lock_time is None or current_time >= lock_time

            if not is_active:
                new_key = key_manager.get_available_key(exclude_keys=tried_keys)
                if new_key:
                    logger.info(f"Switching to new key {new_key[:14]}...")
                    apply_upstream_key(headers, new_key)
                    kwargs["headers"] = headers
                    kwargs["selected_key"] = new_key
                    selected_key = new_key
                    tried_keys.add(new_key)
                else:
                    logger.error("All API keys are rate limited")
                    raise HTTPException(
                        status_code=429,
                        detail="All API keys are rate limited.",
                    )

            try:
                yielded_any = False
                async for value in func(*args, **kwargs):
                    yielded_any = True
                    yield value
                break  # Success
            except (HTTPException, httpx.HTTPStatusError, Exception) as e:
                if yielded_any:
                    logger.warning(
                        f"Error mid-stream, cannot retry: {type(e).__name__}: {e}"
                    )
                    raise e

                status_code = 500
                if isinstance(e, HTTPException):
                    status_code = e.status_code
                elif isinstance(e, httpx.HTTPStatusError):
                    status_code = e.response.status_code

                if status_code in (429, 402, 403):
                    response = (
                        e.response if isinstance(e, httpx.HTTPStatusError) else None
                    )
                    if response:
                        await key_manager.handle_rate_limit_response(
                            selected_key, response
                        )

                    logger.info(
                        f"Key {selected_key[:14]}... status {status_code} before stream started, switching..."
                    )

                    new_key = key_manager.get_available_key(exclude_keys=tried_keys)
                    if new_key:
                        apply_upstream_key(headers, new_key)
                        kwargs["headers"] = headers
                        kwargs["selected_key"] = new_key
                    else:
                        raise e

                    last_exception = e
                    await asyncio.sleep(random.uniform(0.1, 0.5))
                elif 400 <= status_code < 500:
                    # Client/upstream validation errors — do not burn more quota retrying
                    raise e
                else:
                    # Transient 5xx: switch to another unused key (still once per key)
                    logger.warning(
                        f"Error before stream started: {type(e).__name__}: {e}. Switching key..."
                    )
                    new_key = key_manager.get_available_key(exclude_keys=tried_keys)
                    if new_key:
                        apply_upstream_key(headers, new_key)
                        kwargs["headers"] = headers
                        kwargs["selected_key"] = new_key
                    last_exception = e
                    await asyncio.sleep(random.uniform(0.5, 1.5))
            except (HTTPException, httpx.HTTPStatusError) as e:
                status_code = (
                    e.status_code
                    if isinstance(e, HTTPException)
                    else e.response.status_code
                )
                if status_code in (429, 402, 403):
                    response = None
                    if isinstance(e, httpx.HTTPStatusError):
                        response = e.response
                    if response:
                        await key_manager.handle_rate_limit_response(
                            selected_key, response
                        )

                    logger.info(
                        f"Key {selected_key[:14]}... status {status_code}, switching..."
                    )

                    new_key = key_manager.get_available_key(exclude_keys=tried_keys)
                    if new_key:
                        apply_upstream_key(headers, new_key)
                        kwargs["headers"] = headers
                        kwargs["selected_key"] = new_key
                    else:
                        raise e

                    last_exception = e
                    await asyncio.sleep(random.uniform(0.1, 0.5))  # Small jitter
                else:
                    raise
            except Exception as e:
                logger.warning(f"Attempt {attempts} failed: {type(e).__name__}: {e}")
                new_key = key_manager.get_available_key(exclude_keys=tried_keys)
                if new_key:
                    apply_upstream_key(headers, new_key)
                    kwargs["headers"] = headers
                    kwargs["selected_key"] = new_key
                last_exception = e
                await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            raise last_exception

    return wrapper


@non_streaming_retry()
async def make_openrouter_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    params: Dict[str, str],
    timeout: float,
):
    """Makes a non-streaming request to OpenRouter with retry logic"""
    try:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=content,
            params=params,
            timeout=timeout,
        )
        return response
    except httpx.RequestError as e:
        logger.warning(f"Request error: {str(e)}")
        return None


def check_retryable_error(response: httpx.Response) -> bool:
    """Checks if the error in the httpx.Response is retryable based on its content"""
    try:
        error_content = response.json()
        if not isinstance(error_content, dict):
            return False
        error_data = error_content.get("error", {})
        error_message = (
            error_data.get("message", "") if isinstance(error_data, dict) else ""
        )
        error_code = (
            error_data.get("code", None) if isinstance(error_data, dict) else None
        )

        if str(error_code) == "429" and "Provider returned error" in error_message:
            return True

    except json.JSONDecodeError:
        logger.warning("Failed to decode JSON from response body during retry check")
    except Exception as e:
        logger.error(f"Unexpected error checking retryable error: {str(e)}")

    return False




def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Removes sensitive data from headers"""
    sensitive_keys = ["authorization", "apikey", "cookie", "set-cookie"]
    sanitized = {}
    for k, v in headers.items():
        key_lower = k.lower()
        if key_lower in sensitive_keys:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


def mask_key(key: Optional[str]) -> str:
    """Masks an API key for logging"""
    if not key:
        return "None"
    return f"{key[:14]}..."


def _normalize_upstream_path(path: str, upstream_base: str) -> str:
    """Avoid /v1/v1/... when UPSTREAM already ends with /v1 (OpenAI-style Magta).

    Anthropic SDK and ArcReel probe call ``{proxy}/v1/messages`` and ``/v1/models``.
    Text clients call ``{proxy}/chat/completions`` (no leading v1). Only strip when
    the upstream root already includes the version segment.
    """
    cleaned = (path or "").lstrip("/")
    base = (upstream_base or "").rstrip("/").lower()
    if base.endswith("/v1") and (cleaned == "v1" or cleaned.startswith("v1/")):
        cleaned = cleaned[3:].lstrip("/")
    return cleaned


def _is_anthropic_messages_path(path: str) -> bool:
    return (path or "").rstrip("/").endswith("messages")


def _is_empty_chat_completion(content: bytes) -> bool:
    """ModelScope soft rate-limit: HTTP 200 with choices=null and zero usage.

    Image/video OpenAI-style payloads use ``data`` / ``id`` without ``choices``;
    those must not be treated as empty chat completions or the proxy will
    burn keys retrying stream fallbacks against non-chat endpoints.
    Anthropic Messages responses use ``type=message`` + ``content`` — never empty-chat.
    """
    if not content:
        return False
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    # Anthropic Messages API (ModelScope dual-protocol)
    if data.get("type") == "message" or (
        isinstance(data.get("content"), list) and "role" in data
    ):
        return False
    # Successful image generation / similar media payloads
    if isinstance(data.get("data"), list):
        return False
    if data.get("object") in {"list", "video", "image"}:
        return False
    choices = data.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = msg.get("content") or msg.get("reasoning_content") or ""
            if isinstance(text, str) and text.strip():
                return False
            # choices present but no usable text — treat as empty for Magta Flash
            usage = data.get("usage") or {}
            total = usage.get("total_tokens") or 0
            return total == 0
        return False
    usage = data.get("usage") or {}
    total = usage.get("total_tokens")
    # Explicit empty completion (null/[] choices). Zero-token usage is common.
    return choices is None or choices == [] or total == 0


def _aggregate_sse_chat_completion(sse_bytes: bytes, fallback_model: str = "") -> bytes:
    """Collapse Magta/OpenAI SSE into a single non-stream chat.completion JSON."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    model = fallback_model
    finish_reason = "stop"
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    response_id = ""

    for block in sse_bytes.decode("utf-8", errors="ignore").split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        if isinstance(chunk.get("model"), str) and chunk["model"]:
            model = chunk["model"]
        if isinstance(chunk.get("id"), str) and chunk["id"]:
            response_id = chunk["id"]
        if isinstance(chunk.get("usage"), dict):
            usage = {**usage, **chunk["usage"]}
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        if choice0.get("finish_reason"):
            finish_reason = choice0["finish_reason"]
        delta = choice0.get("delta") if isinstance(choice0.get("delta"), dict) else {}
        if isinstance(delta.get("content"), str) and delta["content"]:
            content_parts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            reasoning_parts.append(delta["reasoning_content"])
        message = choice0.get("message") if isinstance(choice0.get("message"), dict) else {}
        if isinstance(message.get("content"), str) and message["content"]:
            content_parts.append(message["content"])
        if isinstance(message.get("reasoning_content"), str) and message["reasoning_content"]:
            reasoning_parts.append(message["reasoning_content"])

    text = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    body = {
        "id": response_id or "chatcmpl-stream-fallback",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or fallback_model or "unknown",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text or reasoning,
                    **({"reasoning_content": reasoning} if reasoning and text else {}),
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


async def _modelscope_stream_fallback_completion(
    *,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    body_bytes: bytes,
    params: dict,
) -> Optional[bytes]:
    """Magta DeepSeek V4 often returns empty non-stream; stream usually works."""
    try:
        body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    model = body.get("model") if isinstance(body.get("model"), str) else ""
    body = {**body, "stream": True}
    stream_headers = {
        **headers,
        "Accept": "text/event-stream",
    }
    try:
        async with client.stream(
            method,
            url,
            headers=stream_headers,
            content=json.dumps(body).encode("utf-8"),
            params=params,
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0),
        ) as response:
            if response.status_code != 200:
                logger.warning(
                    f"Stream fallback upstream status {response.status_code} for model={model}"
                )
                return None
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
            aggregated = _aggregate_sse_chat_completion(bytes(chunks), fallback_model=model)
            if _is_empty_chat_completion(aggregated):
                return None
            return aggregated
    except Exception as e:
        logger.warning(f"Stream fallback failed: {type(e).__name__}: {e}")
        return None


def log_request_debug(
    method: str, url: str, headers: Dict[str, str], body: bytes, prefix: str = ""
):
    """Logs request details in debug mode"""
    sanitized_headers = sanitize_headers(headers)
    body_info = None
    if body:
        try:
            body_info = json.loads(body.decode("utf-8"))
        except:
            body_info = f"Binary data ({len(body)} bytes)"

    log_data = {
        "method": method,
        "url": url,
        "headers": sanitized_headers,
        "body": body_info,
    }

    prefix_str = f"[{prefix}] " if prefix else ""
    logger.debug(
        f"{prefix_str}Request details:\n%s",
        json.dumps(log_data, indent=2, ensure_ascii=False),
    )


def log_response_debug(response: httpx.Response, prefix: str = ""):
    """Logs response details in debug mode"""
    sanitized_headers = sanitize_headers(dict(response.headers))

    log_data = {
        "status_code": response.status_code,
        "headers": sanitized_headers,
    }

    prefix_str = f"[{prefix}] " if prefix else ""
    logger.debug(
        f"{prefix_str}Response details:\n%s",
        json.dumps(log_data, indent=2, ensure_ascii=False),
    )


class KeyManager:
    """Manages upstream API keys, including rotation, rate limiting, and usage tracking"""

    def __init__(self):
        self.active_key: Optional[str] = UPSTREAM_KEYS[0] if UPSTREAM_KEYS else None
        self.key_configs = {c["key"]: c for c in UPSTREAM_CONFIG}
        self.usage_stats = {
            key: {
                "daily_count": 0,
                "last_request_at": None,
                "reset_at": self._get_next_reset(),
            }
            for key in UPSTREAM_KEYS
        }

    def _get_next_reset(self) -> pendulum.DateTime:
        """Next daily quota reset at midnight in TIMEZONE (UTC for OpenRouter, Asia/Shanghai for ModelScope)."""
        return pendulum.today(TIMEZONE).add(days=1)

    def _check_and_reset_quotas(self):
        """Checks if quotas need to be reset based on the current time"""
        now = pendulum.now(TIMEZONE)
        for key in UPSTREAM_KEYS:
            if now >= self.usage_stats[key]["reset_at"]:
                logger.info(f"Resetting daily quota for key {key[:14]}...")
                self.usage_stats[key]["daily_count"] = 0
                self.usage_stats[key]["reset_at"] = self._get_next_reset()
                if key_status.get(key) and key_status[key] > now:
                    # If it was blocked until midnight, clear it
                    key_status[key] = None

    def _remaining_quota(self, key: str) -> int:
        config = self.key_configs[key]
        return config["limit"] - self.usage_stats[key]["daily_count"]

    def _is_key_available(
        self,
        key: str,
        current_time: pendulum.DateTime,
        excluded: set,
    ) -> bool:
        if key in excluded:
            return False

        # 1. Check if blocked by 429/402/403
        lock_time = key_status.get(key)
        if lock_time and current_time < lock_time:
            return False

        stats = self.usage_stats[key]
        config = self.key_configs[key]

        # 2. Check daily limit
        if stats["daily_count"] >= config["limit"]:
            return False

        # 3. Optional min interval (OpenRouter free: ~20 RPM => 3s)
        if KEY_MIN_INTERVAL_SECONDS > 0 and stats["last_request_at"]:
            seconds_since_last = (
                current_time - stats["last_request_at"]
            ).total_seconds()
            if seconds_since_last < KEY_MIN_INTERVAL_SECONDS:
                return False

        return True

    def _list_available_keys(
        self, exclude_keys: Optional[set] = None
    ) -> List[str]:
        if not UPSTREAM_KEYS:
            return []
        self._check_and_reset_quotas()
        current_time = pendulum.now(TIMEZONE)
        excluded = exclude_keys or set()
        return [
            key
            for key in UPSTREAM_KEYS
            if self._is_key_available(key, current_time, excluded)
        ]

    def has_available_key(self, exclude_keys: Optional[set] = None) -> bool:
        """True if any key can be selected (does not reserve quota)."""
        return bool(self._list_available_keys(exclude_keys))

    def get_available_key(
        self, exclude_keys: Optional[set] = None
    ) -> Optional[str]:
        """Sticky selection: keep active key while usable; otherwise pick highest remaining quota.

        exclude_keys: keys already tried for the current client request (at most once each).
        """
        candidates = self._list_available_keys(exclude_keys)
        if not candidates:
            return None

        current_time = pendulum.now(TIMEZONE)
        if self.active_key in candidates:
            selected_key = self.active_key
        else:
            # Switch only when sticky key is unusable; prefer absolute remaining quota
            selected_key = max(candidates, key=self._remaining_quota)
            prev = f"{self.active_key[:14]}..." if self.active_key else "none"
            logger.info(
                f"Sticky key switch {prev} -> {selected_key[:14]}... "
                f"(remaining {self._remaining_quota(selected_key)}/{self.key_configs[selected_key]['limit']})"
            )

        self.active_key = selected_key

        # Update stats immediately to "reserve" the slot
        self.usage_stats[selected_key]["last_request_at"] = current_time
        self.usage_stats[selected_key]["daily_count"] += 1

        logger.debug(
            f"Selected key {selected_key[:14]}... (Usage: {self.usage_stats[selected_key]['daily_count']}/{self.key_configs[selected_key]['limit']})"
        )
        return selected_key

    def block_key_until_next_day(self, key: str):
        """Blocks a key until the next daily reset (e.g., when daily limit is reached)"""
        unlock_time = self._get_next_reset().in_timezone(TIMEZONE)
        key_status[key] = unlock_time

        # Sync internal counter to limit
        if key in self.usage_stats:
            self.usage_stats[key]["daily_count"] = self.key_configs[key]["limit"]

        logger.warning(
            f"Key {key[:14]}... blocked until {unlock_time.to_iso8601_string()} (Daily limit reached)"
        )

    async def handle_rate_limit_response(self, key: str, response: httpx.Response):
        """Handles 429 and 402 responses by blocking keys for appropriate durations"""
        try:
            if response.status_code == 402:
                # Payment Required - block for 5 minutes and warn user
                unlock_time = pendulum.now(TIMEZONE).add(minutes=5)
                key_status[key] = unlock_time
                logger.warning(
                    f"Key {key[:14]}... returned 402 Payment Required. Blocked for 5m. Please check your BYOK credits!"
                )
                return

            if response.status_code == 403:
                unlock_time = pendulum.now(TIMEZONE).add(minutes=30)
                key_status[key] = unlock_time
                logger.warning(
                    f"Key {key[:14]}... returned 403. Blocked for 30m (likely key limit exceeded)."
                )
                return

            try:
                error_content_bytes = await response.aread()
                error_content = json.loads(error_content_bytes.decode("utf-8"))
            except json.decoder.JSONDecodeError:
                error_content = None

            # Non-OpenRouter upstreams (e.g. ModelScope)
            if not IS_OPENROUTER:
                if response.status_code == 429:
                    err_msg = ""
                    if isinstance(error_content, dict):
                        err = error_content.get("error") or error_content.get("detail") or ""
                        if isinstance(err, dict):
                            err_msg = str(err.get("message") or "")
                        else:
                            err_msg = str(err)
                    # Only treat explicit daily-quota wording as end-of-day; model soft-limits are temporary
                    if re.search(
                        r"free-models-per-day|daily.?limit|quota.?exceeded|insufficient.?quota|今日|天限额",
                        err_msg,
                        re.I,
                    ):
                        self.block_key_until_next_day(key)
                        logger.warning(
                            f"Key {key[:14]}... daily quota 429 — blocked until next day ({TIMEZONE}). ({err_msg[:120]})"
                        )
                    else:
                        unlock_time = pendulum.now(TIMEZONE).add(seconds=60)
                        key_status[key] = unlock_time
                        logger.warning(
                            f"Key {key[:14]}... ModelScope 429 soft-limit. Blocked for 60s. ({err_msg[:120] or 'no body'})"
                        )
                else:
                    logger.warning(
                        f"Key {key[:14]}... received status {response.status_code}"
                    )
                return

            if error_content and isinstance(error_content, dict):
                error_data = error_content.get("error", {})
                error_message = (
                    error_data.get("message", "")
                    if isinstance(error_data, dict)
                    else ""
                )
                error_code = (
                    error_data.get("code", None)
                    if isinstance(error_data, dict)
                    else None
                )

                if str(error_code) == "429" or response.status_code == 429:
                    if (
                        "free-models-per-day" in error_message
                        or "Credits exhausted" in error_message
                    ):
                        self.block_key_until_next_day(key)
                        logger.warning(
                            f"Key {key[:14]}... blocked until next day: {error_message}"
                        )
                    elif "Provider returned error" in error_message:
                        # Temporary model-level rate limit from upstream provider
                        unlock_time = pendulum.now(TIMEZONE).add(seconds=30)
                        key_status[key] = unlock_time
                        logger.warning(
                            f"Key {key[:14]}... upstream provider rate-limit. Blocked for 30s."
                        )
                    else:
                        # Other 429 (e.g. OpenRouter's own rate limit for the key)
                        unlock_time = pendulum.now(TIMEZONE).add(seconds=10)
                        key_status[key] = unlock_time
                        logger.warning(
                            f"Key {key[:14]}... OpenRouter rate-limit. Blocked for 10s."
                        )
                else:
                    logger.warning(
                        f"Key {key[:14]}... received error {error_code}: {error_message}"
                    )
            else:
                if response.status_code == 429:
                    unlock_time = pendulum.now(TIMEZONE).add(seconds=10)
                    key_status[key] = unlock_time
                    logger.warning(
                        f"Key {key[:14]}... received 429 without JSON body. Blocked for 10s."
                    )
                else:
                    logger.warning(
                        f"Key {key[:14]}... received status {response.status_code}. Response body is not JSON."
                    )

        except Exception as e:
            logger.error(f"Unexpected error handling rate limit response: {str(e)}")

    def get_key_statuses(self) -> Dict[str, Dict]:
        """Returns the current status and usage statistics for all API keys"""
        current_time = pendulum.now(TIMEZONE)
        statuses = {}
        for key in UPSTREAM_KEYS:
            lock_time = key_status.get(key)
            stats = self.usage_stats[key]
            config = self.key_configs[key]

            status_str = "active"
            if lock_time and current_time < lock_time:
                status_str = f"blocked_until_{lock_time.to_iso8601_string()}"
            elif stats["daily_count"] >= config["limit"]:
                status_str = "daily_limit_reached"

            statuses[key] = {
                "status": status_str,
                "usage": f"{stats['daily_count']}/{config['limit']}",
                "used": stats["daily_count"],
                "limit": config["limit"],
                "remaining": config["limit"] - stats["daily_count"],
                "last_use": (
                    stats["last_request_at"].to_iso8601_string()
                    if stats["last_request_at"]
                    else None
                ),
            }
        return statuses

    def get_dashboard_payload(self) -> Dict:
        """Masked key list + totals for the admin dashboard."""
        raw = self.get_key_statuses()
        items = []
        total_remaining = 0
        total_limit = 0
        active = 0
        for idx, key in enumerate(UPSTREAM_KEYS):
            info = raw[key]
            total_remaining += max(0, info["remaining"])
            total_limit += info["limit"]
            if info["status"] == "active":
                active += 1
            items.append(
                {
                    "index": idx,
                    "masked": mask_key(key),
                    "key": key,
                    "status": info["status"],
                    "used": info["used"],
                    "limit": info["limit"],
                    "remaining": info["remaining"],
                    "usage": info["usage"],
                    "last_use": info["last_use"],
                }
            )
        profile = (
            "openrouter"
            if IS_OPENROUTER
            else ("anthropic" if IS_ANTHROPIC else "modelscope")
        )
        return {
            "profile": profile,
            "upstream": UPSTREAM_BASE_URL,
            "timezone": TIMEZONE,
            "key_count": len(UPSTREAM_KEYS),
            "active_keys": active,
            "total_remaining": total_remaining,
            "total_limit": total_limit,
            "keys": items,
        }


key_manager = KeyManager()


def _extract_proxy_api_key(
    authorization: Optional[str] = None,
    apikey: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> Optional[str]:
    if apikey:
        return apikey
    if x_api_key:
        return x_api_key
    if authorization and authorization.startswith("Bearer "):
        return authorization.split(" ", 1)[1].strip()
    return None


def _require_proxy_api_key(
    authorization: Optional[str] = None,
    apikey: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> None:
    token = _extract_proxy_api_key(authorization, apikey, x_api_key)
    if token != PROXY_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid proxy API key")


async def test_upstream_key(key: str) -> Dict:
    """Lightweight live check against upstream /models (does not burn daily quota)."""
    if not http_client:
        return {
            "ok": False,
            "status_code": None,
            "elapsed_s": 0,
            "detail": "HTTP client not ready",
        }
    headers = apply_upstream_key(
        {"Accept": "application/json"},
        key,
    )
    url = f"{UPSTREAM_BASE_URL}/models"
    t0 = time.time()
    try:
        resp = await http_client.get(url, headers=headers, timeout=30.0)
        elapsed = round(time.time() - t0, 2)
        body_preview = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                models = data.get("data") or data.get("models") or []
                count = len(models) if isinstance(models, list) else None
                err = data.get("error") or data.get("message")
                body_preview = json.dumps(
                    {"models": count, "error": err}, ensure_ascii=False
                )[:240]
            else:
                body_preview = str(data)[:240]
        except Exception:
            body_preview = (resp.text or "")[:240]
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "elapsed_s": elapsed,
            "detail": body_preview,
        }
    except Exception as e:
        return {
            "ok": False,
            "status_code": None,
            "elapsed_s": round(time.time() - t0, 2),
            "detail": str(e),
        }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Proxy Keys</title>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a222c;
    --text: #e7ecf1;
    --muted: #8b9aab;
    --ok: #3dd68c;
    --warn: #f5a524;
    --bad: #f31260;
    --line: #2a3542;
    --accent: #6ea8fe;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg); color: var(--text); padding: 24px;
  }
  h1 { margin: 0 0 4px; font-size: 1.4rem; font-weight: 650; }
  .sub { color: var(--muted); font-size: 0.9rem; margin-bottom: 20px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
  input[type=password], input[type=text], select {
    background: var(--panel); border: 1px solid var(--line); color: var(--text);
    border-radius: 8px; padding: 10px 12px; min-width: 280px;
  }
  select { min-width: 420px; max-width: 100%; }
  button {
    background: var(--accent); color: #081018; border: 0; border-radius: 8px;
    padding: 10px 14px; font-weight: 600; cursor: pointer;
  }
  button.secondary { background: #2b3644; color: var(--text); }
  button:disabled { opacity: 0.5; cursor: wait; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px;
  }
  .card .label { color: var(--muted); font-size: 0.8rem; }
  .card .value { font-size: 1.6rem; font-weight: 700; margin-top: 6px; }
  .selected-box {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 16px; margin-bottom: 18px; display: none;
  }
  .selected-box.show { display: block; }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 12px; overflow: hidden; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--line); font-size: 0.92rem; }
  th { color: var(--muted); font-weight: 600; background: #151c24; }
  tr.selected { background: rgba(110,168,254,.08); }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  }
  .pill.ok { background: rgba(61,214,140,.15); color: var(--ok); }
  .pill.bad { background: rgba(243,18,96,.15); color: var(--bad); }
  .pill.warn { background: rgba(245,165,36,.15); color: var(--warn); }
  .mono { font-family: ui-monospace, Consolas, monospace; }
  .msg { color: var(--muted); min-height: 1.2em; margin-bottom: 8px; }
  .test-ok { color: var(--ok); }
  .test-bad { color: var(--bad); }
</style>
</head>
<body>
  <h1>上游 Key 面板</h1>
  <div class="sub" id="meta">加载中…</div>

  <div class="row">
    <input id="apiKey" type="password" placeholder="PROXY_API_KEY（代理客户端 Key）" />
    <button id="btnSave">保存并刷新</button>
    <button class="secondary" id="btnRefresh">刷新</button>
    <button class="secondary" id="btnTestAll">测试全部 Key</button>
  </div>

  <div class="row">
    <label for="keySelect" style="color:var(--muted)">选择 Key</label>
    <select id="keySelect">
      <option value="">加载后自动填入全部 Key…</option>
    </select>
    <button class="secondary" id="btnTestSelected">测试选中</button>
  </div>
  <div class="msg" id="msg"></div>

  <div class="cards" id="cards"></div>
  <div class="selected-box" id="selectedBox"></div>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Key</th><th>状态</th><th>已用</th><th>剩余</th><th>上限</th><th>上次使用</th><th>测试</th><th></th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

<script>
const LS = 'proxy_dashboard_api_key';
const PREFILL = __PREFILL_PROXY_API_KEY__;
const apiKeyEl = document.getElementById('apiKey');
const keySelect = document.getElementById('keySelect');
const msg = document.getElementById('msg');
const meta = document.getElementById('meta');
const tbody = document.getElementById('tbody');
const cards = document.getElementById('cards');
const selectedBox = document.getElementById('selectedBox');
apiKeyEl.value = localStorage.getItem(LS) || PREFILL || '';

let keysCache = [];
let testCache = {};

function authHeaders() {
  const k = apiKeyEl.value.trim();
  return { 'APIKEY': k, 'Authorization': 'Bearer ' + k, 'Content-Type': 'application/json' };
}
function setMsg(t, ok) {
  msg.textContent = t || '';
  msg.style.color = ok === false ? 'var(--bad)' : (ok === true ? 'var(--ok)' : 'var(--muted)');
}
function statusPill(s) {
  if (s === 'active') return '<span class="pill ok">active</span>';
  if (String(s).includes('daily')) return '<span class="pill warn">daily_limit</span>';
  return '<span class="pill bad">' + s + '</span>';
}
function optionLabel(item) {
  const t = testCache[item.index];
  const tag = t ? (t.ok ? ' · OK' : ' · FAIL') : '';
  const full = item.key || item.masked;
  return '#' + (item.index + 1) + '  ' + full + '  · 剩余 ' + item.remaining + '/' + item.limit + tag;
}
function fillKeySelect(preferIndex) {
  const prev = preferIndex != null ? String(preferIndex) : keySelect.value;
  keySelect.innerHTML = '';
  if (!keysCache.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '暂无 Key（请点保存并刷新）';
    keySelect.appendChild(o);
    return;
  }
  keysCache.forEach(item => {
    const o = document.createElement('option');
    o.value = String(item.index);
    o.textContent = optionLabel(item);
    o.dataset.key = item.key || '';
    keySelect.appendChild(o);
  });
  if (prev !== '' && keysCache.some(k => String(k.index) === prev)) {
    keySelect.value = prev;
  } else {
    keySelect.value = String(keysCache[0].index);
  }
  renderSelected();
}
function renderSelected() {
  const idx = keySelect.value === '' ? -1 : Number(keySelect.value);
  const item = keysCache.find(k => k.index === idx);
  tbody.querySelectorAll('tr').forEach(tr => {
    tr.classList.toggle('selected', Number(tr.dataset.index) === idx);
  });
  if (!item) {
    selectedBox.classList.remove('show');
    selectedBox.innerHTML = '';
    return;
  }
  const t = testCache[item.index];
  const testText = t
    ? ((t.ok ? 'OK' : 'FAIL') + ' ' + (t.status_code ?? '') + ' ' + (t.elapsed_s ?? '') + 's')
    : '未测试';
  const full = item.key || item.masked;
  selectedBox.classList.add('show');
  selectedBox.innerHTML =
    '<div><strong>当前选中</strong> #' + (item.index + 1) + '</div>' +
    '<div class="mono" style="margin-top:6px;word-break:break-all">' + full + '</div>' +
    '<div style="margin-top:8px;color:var(--muted)">状态 ' + statusPill(item.status) +
    ' · 剩余 <strong style="color:var(--text)">' + item.remaining + '</strong>/' + item.limit +
    ' · 测试 <span class="mono">' + testText + '</span></div>' +
    '<div class="row" style="margin:10px 0 0 0"><button class="secondary" id="btnCopyKey">复制 Key</button></div>';
  const copyBtn = document.getElementById('btnCopyKey');
  if (copyBtn) {
    copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(full);
        setMsg('已复制 Key #' + (item.index + 1), true);
      } catch (e) {
        setMsg('复制失败，请手动选择', false);
      }
    };
  }
}

async function loadStatus() {
  const k = apiKeyEl.value.trim();
  if (!k) { setMsg('请先填写 PROXY_API_KEY（默认 modelscope_proxy_api_key）', false); return; }
  localStorage.setItem(LS, k);
  setMsg('刷新中…');
  const r = await fetch('/api/status', { headers: authHeaders() });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { setMsg(d.detail || ('HTTP ' + r.status), false); return; }
  meta.textContent = d.profile + ' · ' + d.upstream + ' · TZ ' + d.timezone + ' · ' + (d.key_count || 0) + ' keys';
  cards.innerHTML = `
    <div class="card"><div class="label">剩余总量</div><div class="value">${d.total_remaining}</div></div>
    <div class="card"><div class="label">日上限总量</div><div class="value">${d.total_limit}</div></div>
    <div class="card"><div class="label">可用 Key</div><div class="value">${d.active_keys}/${d.key_count}</div></div>`;
  keysCache = d.keys || [];
  fillKeySelect();
  tbody.innerHTML = keysCache.map(item => `
    <tr data-index="${item.index}">
      <td>${item.index + 1}</td>
      <td class="mono" style="word-break:break-all">${item.key || item.masked}</td>
      <td>${statusPill(item.status)}</td>
      <td>${item.used}</td>
      <td><strong>${item.remaining}</strong></td>
      <td>${item.limit}</td>
      <td class="mono">${item.last_use || '-'}</td>
      <td class="test-cell mono">—</td>
      <td><button class="secondary btn-test" data-index="${item.index}">测试</button></td>
    </tr>`).join('');
  document.querySelectorAll('.btn-test').forEach(btn => {
    btn.onclick = () => {
      keySelect.value = String(btn.dataset.index);
      renderSelected();
      testOne(Number(btn.dataset.index));
    };
  });
  Object.keys(testCache).forEach(i => {
    const item = testCache[i];
    const row = tbody.querySelector('tr[data-index="' + i + '"] .test-cell');
    if (!row || !item) return;
    row.className = 'test-cell mono ' + (item.ok ? 'test-ok' : 'test-bad');
    row.textContent = (item.ok ? 'OK' : 'FAIL') + ' ' + (item.status_code ?? '') + ' ' + (item.elapsed_s ?? '') + 's';
    row.title = item.detail || '';
  });
  renderSelected();
  setMsg('已写入下拉 ' + keysCache.length + ' 个 Key · ' + new Date().toLocaleTimeString(), true);
}

async function testOne(index) {
  setMsg('测试 Key #' + (index + 1) + '…');
  const r = await fetch('/api/test-key', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ index })
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { setMsg(d.detail || ('HTTP ' + r.status), false); return; }
  testCache[index] = d;
  const row = tbody.querySelector('tr[data-index="' + index + '"] .test-cell');
  if (row) {
    row.className = 'test-cell mono ' + (d.ok ? 'test-ok' : 'test-bad');
    row.textContent = (d.ok ? 'OK' : 'FAIL') + ' ' + (d.status_code ?? '') + ' ' + (d.elapsed_s ?? '') + 's';
    row.title = d.detail || '';
  }
  fillKeySelect(index);
  setMsg(d.ok ? ('Key #' + (index + 1) + ' 可用') : ('Key #' + (index + 1) + ' 失败'), d.ok);
}

async function testAll() {
  setMsg('测试全部 Key…');
  const r = await fetch('/api/test-keys', { method: 'POST', headers: authHeaders() });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { setMsg(d.detail || ('HTTP ' + r.status), false); return; }
  (d.results || []).forEach(item => {
    testCache[item.index] = item;
    const row = tbody.querySelector('tr[data-index="' + item.index + '"] .test-cell');
    if (!row) return;
    row.className = 'test-cell mono ' + (item.ok ? 'test-ok' : 'test-bad');
    row.textContent = (item.ok ? 'OK' : 'FAIL') + ' ' + (item.status_code ?? '') + ' ' + (item.elapsed_s ?? '') + 's';
    row.title = item.detail || '';
  });
  fillKeySelect();
  const okN = (d.results || []).filter(x => x.ok).length;
  setMsg('测试完成：' + okN + '/' + (d.results || []).length + ' 通过，已写入下拉', okN > 0);
}

document.getElementById('btnSave').onclick = loadStatus;
document.getElementById('btnRefresh').onclick = loadStatus;
document.getElementById('btnTestAll').onclick = testAll;
document.getElementById('btnTestSelected').onclick = () => {
  if (keySelect.value === '') { setMsg('请先选择 Key', false); return; }
  testOne(Number(keySelect.value));
};
keySelect.onchange = renderSelected;
if (apiKeyEl.value.trim()) loadStatus();
setInterval(() => { if (apiKeyEl.value.trim()) loadStatus(); }, 15000);
</script>
</body>
</html>
"""


@app.get("/health")
async def health_endpoint(format: Optional[str] = None):
    """Simple health check endpoint"""
    if format and format.lower() == "json":
        return JSONResponse(content={"status": "OK"}, status_code=200)
    return Response(content="OK", status_code=200)


@app.get("/key-status")
async def get_key_status(apikey: Annotated[str, Header(alias="APIKEY")]):
    """Endpoint to retrieve the status of all managed API keys"""
    if apikey != PROXY_API_KEY:
        logger.warning("Invalid proxy API key for key-status endpoint")
        raise HTTPException(status_code=403, detail="Invalid proxy API key")
    return KeyStatusResponse(keys=key_manager.get_key_statuses())


@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Admin page: remaining quotas + live key tests."""
    # Local admin: prefill client key so the 7 upstream keys load immediately.
    html = DASHBOARD_HTML.replace(
        "__PREFILL_PROXY_API_KEY__",
        json.dumps(PROXY_API_KEY or "", ensure_ascii=False),
    )
    return HTMLResponse(content=html)


@app.get("/api/status")
async def api_status(
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    apikey: Annotated[Optional[str], Header(alias="APIKEY")] = None,
    x_api_key: Annotated[Optional[str], Header(alias="x-api-key")] = None,
):
    _require_proxy_api_key(authorization, apikey, x_api_key)
    return key_manager.get_dashboard_payload()


class TestKeyRequest(BaseModel):
    index: int


@app.post("/api/test-key")
async def api_test_key(
    body: TestKeyRequest,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    apikey: Annotated[Optional[str], Header(alias="APIKEY")] = None,
    x_api_key: Annotated[Optional[str], Header(alias="x-api-key")] = None,
):
    _require_proxy_api_key(authorization, apikey, x_api_key)
    if body.index < 0 or body.index >= len(UPSTREAM_KEYS):
        raise HTTPException(status_code=400, detail="Invalid key index")
    key = UPSTREAM_KEYS[body.index]
    result = await test_upstream_key(key)
    return {"index": body.index, "masked": mask_key(key), **result}


@app.post("/api/test-keys")
async def api_test_keys(
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    apikey: Annotated[Optional[str], Header(alias="APIKEY")] = None,
    x_api_key: Annotated[Optional[str], Header(alias="x-api-key")] = None,
):
    _require_proxy_api_key(authorization, apikey, x_api_key)
    results = []
    for idx, key in enumerate(UPSTREAM_KEYS):
        result = await test_upstream_key(key)
        results.append({"index": idx, "masked": mask_key(key), **result})
    return {"results": results}


@async_retryable
async def forward_streaming(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    params: Dict[str, str],
    selected_key: str,
) -> AsyncGenerator[bytes, None]:
    """Asynchronously forwards streaming response with backoff"""
    masked_key_str = mask_key(selected_key)
    # Free/heavy models (e.g. nemotron-3-ultra) often take >10s before first SSE chunk.
    stream_timeout = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
    try:
        async with client.stream(
            method=method,
            url=url,
            headers=headers,
            content=content,
            params=params,
            timeout=stream_timeout,
        ) as response:
            log_response_debug(response, prefix=masked_key_str)
            if response.status_code in (429, 402, 403):
                await key_manager.handle_rate_limit_response(selected_key, response)
                raise HTTPException(
                    status_code=response.status_code,
                    detail=(
                        "Rate limited"
                        if response.status_code == 429
                        else (
                            "Payment Required"
                            if response.status_code == 402
                            else "Key limit exceeded"
                        )
                    ),
                )

            if response.status_code != 200:
                try:
                    error_body = await response.aread()
                    error_detail = (
                        error_body.decode("utf-8")
                        if error_body
                        else "OpenRouter API error"
                    )
                except Exception as e:
                    error_detail = f"Failed to read error body: {str(e)}"

                logger.error(
                    f"[{masked_key_str}] Upstream error: {response.status_code} - {error_detail}"
                )
                raise HTTPException(
                    status_code=response.status_code, detail=error_detail
                )

            # This will be logged in proxy_request
            logger.debug(
                f"[{masked_key_str}] Response headers: {sanitize_headers(dict(response.headers))}"
            )

            async for chunk in response.aiter_bytes():
                yield chunk

    except httpx.HTTPStatusError as e:
        logger.error(f"[{masked_key_str}] HTTP error: {str(e)}")
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"[{masked_key_str}] Request failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Upstream API unavailable")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def proxy_request(
    request: Request,
    path: str,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
    apikey: Annotated[Optional[str], Header(alias="APIKEY")] = None,
    x_api_key: Annotated[Optional[str], Header(alias="x-api-key")] = None,
):
    """Main proxy endpoint that forwards requests upstream with key management and retries"""
    # Local UI / probe paths must never hit upstream auth
    local_paths = {
        "",
        "dashboard",
        "health",
        "docs",
        "openapi.json",
        "redoc",
        "favicon.ico",
        "api/status",
        "api/test-key",
        "api/test-keys",
        "key-status",
    }
    if path in local_paths or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")

    # Handle CORS preflight requests locally and don't forward them upstream
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, APIKEY, x-api-key, Content-Type",
            },
        )

    # Anthropic SDK / Claude Agent 默认发 x-api-key；OpenAI 客户端常用 Bearer / APIKEY。
    valid_auth = False
    if apikey and apikey == PROXY_API_KEY:
        valid_auth = True
    elif x_api_key and x_api_key == PROXY_API_KEY:
        valid_auth = True
    elif (
        authorization
        and authorization.startswith("Bearer ")
        and authorization.split(" ")[1] == PROXY_API_KEY
    ):
        valid_auth = True

    if not valid_auth:
        logger.warning("Invalid authentication attempt")
        raise HTTPException(status_code=403, detail="Invalid authentication")

    is_streaming = False
    body_bytes = await request.body()
    path = _normalize_upstream_path(path, UPSTREAM_BASE_URL)
    is_anthropic_messages = _is_anthropic_messages_path(path)

    # Streaming when the body asks for it on OpenAI chat/completions *or*
    # Anthropic Messages (/v1/messages). Agent SDK relies on the latter.
    if request.method == "POST" and (
        path.endswith("completions") or is_anthropic_messages
    ):
        try:
            if body_bytes:
                request_body = json.loads(body_bytes)
                is_streaming = bool(request_body.get("stream", False))
        except json.JSONDecodeError:
            logger.warning("Failed to parse request body as JSON")

    # Prepare request to upstream (OpenRouter, Anthropic, ModelScope, …)
    upstream_url = (
        f"{UPSTREAM_BASE_URL}/{path}" if path else UPSTREAM_BASE_URL
    )
    params = dict(request.query_params)

    start_time = time.time()

    # Preserve Anthropic protocol headers from the client (version / beta).
    incoming = {k.lower(): v for k, v in request.headers.items()}

    def build_upstream_headers(
        selected_key: str, *, streaming: bool
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
        }
        if IS_ANTHROPIC:
            # Official Anthropic Messages API authenticates with x-api-key.
            headers["x-api-key"] = selected_key
            headers["anthropic-version"] = incoming.get(
                "anthropic-version", "2023-06-01"
            )
            if "anthropic-beta" in incoming:
                headers["anthropic-beta"] = incoming["anthropic-beta"]
            headers["X-Title"] = "AnthropicKeyRotator"
        else:
            headers["Authorization"] = f"Bearer {selected_key}"
            # ModelScope dual-protocol: Anthropic Messages still needs version header.
            if is_anthropic_messages:
                headers["anthropic-version"] = incoming.get(
                    "anthropic-version", "2023-06-01"
                )
                if "anthropic-beta" in incoming:
                    headers["anthropic-beta"] = incoming["anthropic-beta"]
            if IS_OPENROUTER:
                headers["X-Title"] = "OpenrouterProxy"
                if streaming:
                    headers["Origin"] = "https://openrouter.ai"
                    headers["Referer"] = "https://openrouter.ai"
                    headers["User-Agent"] = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
                    )
            else:
                headers["X-Title"] = "UpstreamProxy"
        return headers

    # For streaming requests, use special handling
    if is_streaming:
        selected_key = key_manager.get_available_key()
        if not selected_key:
            logger.error("All API keys are rate limited")
            raise HTTPException(
                status_code=429,
                detail="All API keys are rate limited.",
            )

        masked_key_str = mask_key(selected_key)
        logger.info(
            f"[{masked_key_str}] Forwarding request upstream: {request.method} {upstream_url}"
        )
        logger.debug(f"[{masked_key_str}] Streaming: {is_streaming}")
        logger.debug(f"[{masked_key_str}] Request body: {body_bytes.decode('utf-8')}")

        upstream_headers = build_upstream_headers(selected_key, streaming=True)

        async def streaming_generator():
            nonlocal start_time
            if not http_client:
                logger.error(
                    f"[{mask_key(selected_key)}] Global HTTP client not initialized"
                )
                yield json.dumps(
                    {"error": {"message": "Internal server error", "code": 500}}
                ).encode("utf-8")
                return

            try:
                async for chunk in forward_streaming(
                    client=http_client,
                    method=request.method,
                    url=upstream_url,
                    headers=upstream_headers,
                    content=body_bytes,
                    params=params,
                    selected_key=selected_key,
                ):
                    if start_time:
                        duration = time.time() - start_time
                        logger.info(
                            f"[{mask_key(selected_key)}] Upstream response: 200 (Streaming started) in {duration:.2f}s"
                        )
                        start_time = None  # Only log once
                    yield chunk
            except HTTPException as e:
                if start_time:
                    duration = time.time() - start_time
                    logger.info(
                        f"[{masked_key_str}] Upstream response: {e.status_code} in {duration:.2f}s"
                    )

                error_data = json.dumps(
                    {
                        "error": {
                            "message": e.detail,
                            "type": "api_error",
                            "code": e.status_code,
                        }
                    }
                ).encode("utf-8")
                yield error_data
            except Exception as e:
                if start_time:
                    duration = time.time() - start_time
                    logger.info(
                        f"[{masked_key_str}] Upstream response: 500 in {duration:.2f}s"
                    )

                logger.error(f"[{masked_key_str}] Unexpected error: {str(e)}")
                error_data = json.dumps(
                    {
                        "error": {
                            "message": "Internal server error",
                            "type": "server_error",
                            "code": 500,
                        }
                    }
                ).encode("utf-8")
                yield error_data

        return StreamingResponse(
            streaming_generator(),
            media_type="text/event-stream",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # Handle non-streaming requests
    if not http_client:
        logger.error("Global HTTP client not initialized")
        raise HTTPException(status_code=500, detail="Internal server error")

    try:
        selected_key = key_manager.get_available_key()
        if not selected_key:
            logger.error("All API keys are rate limited")
            raise HTTPException(
                status_code=429,
                detail="All API keys are rate limited.",
            )

        masked_key_str = mask_key(selected_key)
        logger.info(
            f"[{masked_key_str}] Forwarding request upstream: {request.method} {upstream_url}"
        )
        logger.debug(f"[{masked_key_str}] Streaming: {is_streaming}")
        logger.debug(f"[{masked_key_str}] Request body: {body_bytes.decode('utf-8')}")

        headers = build_upstream_headers(selected_key, streaming=False)

        response = await make_openrouter_request(
            client=http_client,
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body_bytes,
            params=params,
            timeout=300,
        )

        if not response:
            raise HTTPException(status_code=502, detail="Upstream request failed")

        log_response_debug(response, prefix=masked_key_str)

        # Handle request limit and retry with different keys if needed (once per key)
        tried_keys = {selected_key}
        while response.status_code in (429, 402, 403):
            logger.warning(
                f"[{mask_key(selected_key)}] Received {response.status_code} status. "
                f"Tried {len(tried_keys)}/{len(UPSTREAM_KEYS)} keys"
            )
            await key_manager.handle_rate_limit_response(selected_key, response)

            selected_key = key_manager.get_available_key(exclude_keys=tried_keys)
            if not selected_key:
                break

            tried_keys.add(selected_key)
            masked_key_str = mask_key(selected_key)
            logger.info(f"Retrying with key: {masked_key_str}...")
            apply_upstream_key(headers, selected_key)
            response = await make_openrouter_request(
                client=http_client,
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body_bytes,
                params=params,
                timeout=300,
            )
            if not response:
                break
            log_response_debug(response, prefix=masked_key_str)

        if not response:
            raise HTTPException(
                status_code=502, detail="Upstream request failed after retries"
            )

        content = response.content

        duration = time.time() - start_time
        logger.info(
            f"[{masked_key_str}] Upstream response: {response.status_code} in {duration:.2f}s"
        )
        logger.debug(f"[{masked_key_str}] Response body: {content[:500]}...")

        # ModelScope soft-limit: HTTP 200 + choices=null + usage 0.
        # Prefer stream-fallback (Magta Flash often works only with stream) before burning keys.
        # Still at most one try per key for the empty-completion path.
        # Skip for Anthropic Messages — those responses have no OpenAI ``choices``.
        while (
            not is_anthropic_messages
            and response.status_code == 200
            and _is_empty_chat_completion(content)
        ):
            logger.warning(
                f"[{masked_key_str}] Empty chat completion (choices=null/empty). "
                f"Trying stream fallback (tried {len(tried_keys)}/{len(UPSTREAM_KEYS)} keys)"
            )
            fallback = await _modelscope_stream_fallback_completion(
                client=http_client,
                method=request.method,
                url=upstream_url,
                headers=headers,
                body_bytes=body_bytes,
                params=params,
            )
            if fallback:
                logger.info(f"[{masked_key_str}] Stream fallback recovered non-empty completion")
                content = fallback
                break

            logger.warning(
                f"[{masked_key_str}] Stream fallback empty; brief cool-down then switch key"
            )
            # Short cool-down only — do not treat every Magta empty as daily quota burn
            key_status[selected_key] = pendulum.now(TIMEZONE).add(seconds=5)
            selected_key = key_manager.get_available_key(exclude_keys=tried_keys)
            if not selected_key:
                break
            tried_keys.add(selected_key)
            masked_key_str = mask_key(selected_key)
            apply_upstream_key(headers, selected_key)
            response = await make_openrouter_request(
                client=http_client,
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body_bytes,
                params=params,
                timeout=300,
            )
            if not response:
                break
            log_response_debug(response, prefix=masked_key_str)
            content = response.content

        if (
            not is_anthropic_messages
            and response
            and response.status_code == 200
            and _is_empty_chat_completion(content)
            and not key_manager.has_available_key(exclude_keys=tried_keys)
        ):
            # Still empty after fallbacks — return upstream body, don't pretend it's a hard 429
            logger.error(
                f"[{masked_key_str}] Magta returned empty completion after stream fallback"
            )

        if not response:
            raise HTTPException(
                status_code=502, detail="Upstream request failed after empty-response retries"
            )

        # Filter out headers that can cause issues with HTTP/2 or are handled by FastAPI
        excluded_headers = {
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "upgrade",
        }
        response_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in excluded_headers
        }
        response_headers["Access-Control-Allow-Origin"] = "*"

        return Response(
            content=content,
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type", "application/json"),
        )
    except httpx.RequestError as e:
        duration = time.time() - start_time
        # Note: selected_key might not be defined if get_available_key failed,
        # but we are inside the try block after it succeeded.
        logger.info(
            f"[{masked_key_str}] Upstream response: RequestError in {duration:.2f}s"
        )
        logger.error(f"[{masked_key_str}] Request failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Upstream API unavailable")


class KeyStatusResponse(BaseModel):
    keys: Dict[str, Dict]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app)
