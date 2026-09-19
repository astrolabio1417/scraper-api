# Scraper API

Small Flask API that fetches pages through a normal HTTP session first and falls back to a headless stealth browser session when the target is blocked. It also exposes a streaming download endpoint and a session inspection endpoint.

## Features

- `POST /api/fetch` for fetching HTML or JSON responses
- `GET /api/download` for streaming a remote file or response body
- `GET /api/session` for inspecting cached sessions by domain
- Per-domain in-memory session cache for headers and cookies
- Cloudflare-aware fallback using Camoufox (stealth Firefox) and `curl_cffi` TLS impersonation

## Requirements

- Python 3.12+
- Flask
- curl_cffi
- camoufox

## Run Locally

Install dependencies:

```bash
pip install -r requirements.txt
python -m camoufox fetch
```

Start the API:

```bash
python main.py
```

The server listens on `http://0.0.0.0:5001`.

You can enable Flask debug mode with:

```bash
FLASK_DEBUG=1 python main.py
```

## Run With Docker

Build the image:

```bash
docker build -t scraper_api .
```

Run the container:

```bash
docker run --rm -p 5001:5001 scraper_api
```

## API

### POST /api/fetch

Fetches the target URL and returns JSON with the response status, final URL, and parsed data when the body is JSON.

Request body:

```json
{
    "url": "https://example.com/data",
    "params": { "page": 1 },
    "headers": { "Referer": "https://example.com/" },
    "method": "GET",
    "follow_redirects": true
}
```

Example:

```bash
curl -X POST http://localhost:5001/api/fetch \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

### GET /api/download

Streams the remote response directly back to the client.

Example:

```bash
curl -L "http://localhost:5001/api/download?url=https://example.com/file.zip" -o file.zip
```

Query parameters besides `url` are forwarded to the target URL.

### GET /api/session

Returns cached session data.

Get all sessions:

```bash
curl http://localhost:5001/api/session
```

Get one domain:

```bash
curl "http://localhost:5001/api/session?domain=example.com"
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `proxy` | unset | Proxy URL for both paths, e.g. `socks5://user:pass@host:1080` |
| `STEALTH_COOLDOWN_S` | `900` | Seconds to stop launching browsers at a domain after stealth failed |
| `CHALLENGE_WAIT_S` | `30` | Seconds to wait for a challenge to clear in the browser |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Notes

- Session data is stored in memory only and is lost when the process restarts.
- The stealth fallback may launch a headless Firefox and take longer than a normal request.
- The API is intended for scraping-friendly use cases where some targets require browser-based challenge solving.
