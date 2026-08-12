# Changelog

## [Unreleased - available on :latest tag for docker image]
### Changed
- Upstream URL is now configurable via `UPSTREAM_BASE_URL` (defaults to OpenRouter). OpenRouter-specific rate-limit parsing and headers remain when the URL hosts `openrouter.ai`.
- Daily quota reset uses `TIMEZONE` midnight (keep `UTC` for OpenRouter; use `Asia/Shanghai` for ModelScope).

### Added
- Generic OpenAI-compatible upstream support (e.g. ModelScope) via `UPSTREAM_BASE_URL` + `UPSTREAM_KEYS`.
- `OPENROUTER_KEYS` remains supported as an alias for `UPSTREAM_KEYS`.
- `DEFAULT_KEY_DAILY_LIMIT` and `KEY_MIN_INTERVAL_SECONDS` (auto defaults: OpenRouter 50/3s; other upstreams 125/0s).
- `.env.modelscope.sample` for running a second ModelScope instance without changing OpenRouter config.

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
