# Changelog

## [Unreleased - available on :latest tag for docker image]
### Changed
- Upstream URL is now configurable via `UPSTREAM_BASE_URL` (defaults to OpenRouter). OpenRouter-specific rate-limit parsing and headers remain when the URL hosts `openrouter.ai`.
- Daily quota reset uses `TIMEZONE` midnight (keep `UTC` for OpenRouter; use `Asia/Shanghai` for ModelScope).
- Split one-click launchers: `一键启动.bat` starts OpenRouter only; `一键启动-魔搭.bat` starts ModelScope only.
- ModelScope one-click prefers Docker Compose (`modelscope-proxy`, `restart: unless-stopped`) so closing the CMD window no longer kills the proxy; falls back to local Python if Docker is unavailable.
- 429/403 retries are at most once per upstream key (no double-tries on the same key).
- Dashboard at `/dashboard` shows remaining quotas; `/api/test-key` and `/api/test-keys` live-check upstream keys.
- Key rotation is sticky: keep the active key while usable; on switch, pick the key with the highest remaining daily quota.

### Added
- Generic OpenAI-compatible upstream support (e.g. ModelScope) via `UPSTREAM_BASE_URL` + `UPSTREAM_KEYS`.
- `OPENROUTER_KEYS` remains supported as an alias for `UPSTREAM_KEYS`.
- Streaming upstream read timeout raised from 10s to 300s (free Ultra models often idle >10s before first SSE chunk).
- Rotate keys on upstream `403` (e.g. OpenRouter key limit exceeded), not only 429/402.

## [0.1.0]
### Changed
- Refactored key rotation logic from random selection to a quota-aware prioritization strategy.
- Updated `OPENROUTER_KEYS` configuration to support optional daily limits per key (e.g., `KEY:LIMIT`).
- Improved `/key-status` endpoint to provide detailed usage statistics, remaining quotas, and last usage timestamps.
- Optimized `proxy_request` to eliminate redundant key selection and prevent double-counting of requests.
- Improved documentation in `Readme.md`, including an updated architecture diagram and clearer configuration instructions.
- Refined authentication error loggging for better clarity.

### Added
- Proactive RPM (Requests Per Minute) throttling to ensure keys do not exceed the 20 RPM limit.
- Daily quota tracking and automatic reset logic at UTC midnight.
- Intelligent key selection that prioritizes keys with the highest percentage of remaining daily quota.
- Automatic synchronization of internal usage counters when receiving rate-limit errors from the OpenRouter API.
- Enhanced logging for key selection decisions and quota usage monitoring.
- Masked OpenRouter API key logging for all proxied requests to improve traceability.
- Support for optional prefixes in debug logging for better request attribution.
