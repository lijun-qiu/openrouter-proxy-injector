<p align="center">

<b>OpenRouter-Proxy-Injector:</b> OpenRouter API Proxy with key management for heavy usage cases.<br />

Lightweight (~48Mb RAM in container) and smart proxy server for OpenRouter key rotation with automatic mitigation of upstream server rate limits.

</p>

# Readme in your language

[![en](https://img.shields.io/badge/lang-en-blue.svg)](https://github.com/serjs/openrouter-proxy-injector/blob/oss/Readme.md)
[![ru](https://img.shields.io/badge/lang-ru-red.svg)](https://github.com/serjs/openrouter-proxy-injector/blob/oss/docs/Readme-ru.md)

# Service overview

OpenRouter Proxy Injector enables you to use and manage OpenRouter API keys for both DEV and PRODUCTION environments, while also tracking and handling rate limits from upstream providers for both paid and free models.

It’s ideal for "Vibe coding", intensive AI agent usage, or simply developing with the OpenRouter API.

# Features

- **Smart Key Rotation**: Uses a quota-aware strategy to prioritize keys with the most remaining daily capacity.
- **Mixed Key Support**: Use different billing API keys for your agent swarm (supports both free and paid keys).
- **Daily Quota Management**: Manage limits of 50 or 1000 free model requests per day for each API account, enabling nearly unlimited use of Free models through multiple accounts.
- **Proactive Throttling**: Automatically respects the 20 requests-per-minute limit per key to avoid 429 errors.
- **Automatic Retries**: Automatically retry requests until a response is received from the upstream model when hitting upstream service limits (e.g., frequent Google Gemini 429 rate limits during intensive agent usage).
- **Full API Support**: Supports all OpenRouter API methods for model interaction as-is.
- **Streaming Support**: Supports both streaming and non-streaming requests.

# Tech details

https://github.com/user-attachments/assets/cb1afbaa-def3-47cc-85ba-622872e2f501

<details>
  <summary>Technical architecture</summary>
  <p>

  ```mermaid
  graph TD
  A["Client (VSCode / AnythingLLM / Custom Code)"] -->|"Request with APIKEY variable"| B["Proxy Server"]
  B --> C{"Key Manager"}
  C -->|"Filter Keys"| D["Check Blocked / Daily Quota / RPM"]
  D -->|"Select Best"| E["Prioritize by Remaining Quota %"]
  E -->|"No Keys"| F["429 - All keys exhausted"] --> G["Response to client"]
  E -->|"Key Selected"| H["Send request to OpenRouter"]
  H --> I{"Response from OpenRouter"}
  I -->|"200 OK"| J["Successful response"] --> G
  I -->|"429 - Provider error (retryable)"| K["Retry request (up to 10 times)"] --> I
  I -->|"429 - Daily limit reached"| L["Block key until UTC Midnight"] --> M["Retry with new key"] --> C
  I -->|"4XX / 5XX - Other error"| ERR["Return error to client"] --> G
  ```

  </p>
</details>

# App config parameters

|ENV variable|Type|Required|Default|Description|
|------------|----|--------|-------|-----------|
|PROXY_API_KEY|String|True|EMPTY|Your custom unified API key for handling your requests|
|UPSTREAM_KEYS|String|True*|EMPTY|Upstream API keys. Optional limits: `key1:50,key2:125`. Alias: `OPENROUTER_KEYS`.|
|OPENROUTER_KEYS|String|True*|EMPTY|Backward-compatible alias for `UPSTREAM_KEYS`.|
|UPSTREAM_BASE_URL|String|False|`https://openrouter.ai/api/v1`|Any OpenAI-compatible base (e.g. ModelScope `https://api-inference.modelscope.cn/v1`). OpenRouter-specific rate-limit logic applies only when the host contains `openrouter.ai`.|
|DEFAULT_KEY_DAILY_LIMIT|Int|False|50 / 125|Default daily limit when a key has no `:limit`. Auto: 50 for OpenRouter, 125 otherwise.|
|KEY_MIN_INTERVAL_SECONDS|Float|False|3 / 0|Min seconds between requests per key. Auto: 3 for OpenRouter (~20 RPM), 0 otherwise.|
|TIMEZONE|String|False|UTC|Timezone for daily quota reset and key locks. Use `Asia/Shanghai` for ModelScope.|
|UVICORN_PORT|Int|False|9999|Default app port to listen|
|UVICORN_HOST|String|False|0.0.0.0|Default app IP to listen|
|UVICORN_LOG_LEVEL|String|False|info|Set logging level. E.g. `debug` for showing request details, including body and responses. API Keys are obfuscated in debug logs.|

\* One of `UPSTREAM_KEYS` or `OPENROUTER_KEYS` is required.

# Quickstart with Docker

1. [Install](https://docs.docker.com/engine/install/) Docker engine

2. Run container `docker run -it -e PROXY_API_KEY=<RANDOM_UNIQUE_STRONG_KEY> -e OPENROUTER_KEYS=sk...ab,sk...cd -p 9999:9999 ghcr.io/serjs/openrouter-proxy-injector:latest`

3. Set OpenRouter API URL as your docker host URL, e.g. `http://<docker_host_ip>:9999`

4. That's it!

5. Check Openrouter Proxy Injector API and key statuses

  [http://<docker_host_ip>:9999/docs](http://<docker_host_ip>:9999/docs)

  [http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get](http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get)

# Quickstart with docker-compose

1. Copy and edit .env: `cp .env.sample .env`

   Fill `PROXY_API_KEY` and `OPENROUTER_KEYS` as the minimum required parameters.

2. Run `docker compose up -d`

3. That's it!

5. Check Openrouter Proxy Injector API and key statuses

  [http://<docker_host_ip>:9999/docs](http://<docker_host_ip>:9999/docs)

  [http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get](http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get)

# Setting up openrouter-proxy for

<details>
  <summary>VSCode Continue</summary>
  <p>Example configuration and usage instructions for VSCode Continue.</p>
  <img src="./docs/vscode_continue_example.png" alt="VSCode Continue Example">
</details>

<details>
  <summary>LobeChat</summary>
  <p>Example configuration and usage instructions for LobeChat.</p>
  <img src="./docs/lobechat_example_config.png" alt="LobeChat Example Config">
</details>

<details>
  <summary>AnythingLLM</summary>
  <p>Example configuration and usage instructions for AnythingLLM.</p>
  <img src="./docs/anythingllm_example.png" alt="AnythingLLM Example">
</details>

# FAQ

<details>
  <summary>Q: What types of API Rate Limiting exist in OpenRouter?</summary>
  <p>
    A: There are three different limits:
    <ol>
      <li><code>:free</code> models limits: 20 requests per minute, 50 requests per day for &lt;=10 credits and 1000 requests per day for >=10 credits. <a href="https://openrouter.ai/docs/api-reference/limits">Official docs</a></li>
      <li>Cloudflare DDOS Protection Rate limits: Handled by user agent in the current codebase.</li>
      <li>Upstream server rate-limit: Global to OpenRouter upstreams, handled with exponential retries (respects the 20 requests-per-minute limit).</li>
    </ol>
  </p>
</details>

<details>
  <summary>Q: How to know which limits I'm facing?</summary>
  <p>
    A: OpenRouter info in API responses are logged by the proxy injector.
    <ol>
      <li><code>&lt;MODEL_NAME&gt; is temporarily rate-limited upstream. Please retry shortly, or add your own key to accumulate your rate limits</code> - logged as <code>Key sk-12345... is temporarily rate-limited upstream.</code> Retrying 15 times.</li>
      <li><code>Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day</code> - logged as <code>Key sk-12345... has reached the maximum success count and is temporarily blocked.</code> Reached daily <code>:free</code> models requests; blocked until limit reset (00:00 UTC).</li>
    </ol>
  </p>
</details>
