import json
import os
import queue
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from flask import Flask, jsonify, request
from scrapling.fetchers import StealthySession

app = Flask(__name__)

# Channels to communicate safely between Flask threads and the Scraper thread
request_queue = queue.Queue()


def scraper_worker():
    """This background thread completely owns the Scrapling session.

    All fetches happen sequentially within this single thread context.
    """
    print("Initializing persistent Scrapling session in background thread...")

    try:
        # The browser context starts and stays alive inside this specific thread
        with StealthySession(headless=True, solve_cloudflare=True) as scraper_session:
            while True:
                # Block until a new fetch request comes in from the Flask route
                task = request_queue.get()
                if task is None:
                    break

                target_url, fetch_kwargs, reply_queue = task

                try:
                    # Executes safely on the correct thread
                    page = scraper_session.fetch(target_url, **fetch_kwargs)

                    raw_content = page.body
                    if isinstance(raw_content, bytes):
                        raw_content = raw_content.decode("utf-8", errors="ignore")

                    is_json = False
                    json_data = None
                    try:
                        json_data = json.loads(raw_content)
                        is_json = True
                    except ValueError:
                        pass

                    response_payload = {
                        "success": True,
                        "status_code": getattr(page, "status", 200),
                        "url": page.url,
                        "is_json": is_json,
                        "data": json_data if is_json else raw_content,
                    }
                    reply_queue.put((response_payload, 200))

                except Exception as e:
                    print(f"Scraper internal execution error: {str(e)}")
                    reply_queue.put(({"success": False, "error": str(e)}, 500))
                finally:
                    request_queue.task_done()

    except Exception as fatal_err:
        print(f"Fatal error in Scraper Worker Thread: {str(fatal_err)}")


# Spin up the background thread immediately on application startup
worker_thread = threading.Thread(target=scraper_worker, daemon=True)
worker_thread.start()


@app.route("/api/fetch", methods=["POST"])
def fetch_raw_html():
    payload = request.get_json() or {}
    target_url = payload.get("url")

    if not target_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    params = payload.get("params")
    headers = payload.get("headers")
    timeout = payload.get("timeout")

    if params and isinstance(params, dict):
        url_parts = list(urlparse(target_url))
        query = dict(parse_qsl(url_parts[4]))
        query.update(params)
        url_parts[4] = urlencode(query)
        target_url = urlunparse(url_parts)

    fetch_kwargs = {}
    if headers and isinstance(headers, dict):
        fetch_kwargs["headers"] = headers

    if timeout is not None:
        try:
            fetch_kwargs["timeout"] = int(float(timeout) * 1000)
        except (ValueError, TypeError):
            pass

    # Create a localized temporary queue to receive the response back for THIS request
    reply_queue = queue.Queue()

    # Ship the work over to the scraper thread
    request_queue.put((target_url, fetch_kwargs, reply_queue))

    # Wait here until the scraper thread finishes processing the job
    response_payload, status_code = reply_queue.get()

    return jsonify(response_payload), status_code


if __name__ == "__main__":
    is_debug = os.environ.get("FLASK_DEBUG") == "1"
    # Flask can now safely run multi-threaded because it doesn't touch the scraper directly
    app.run(host="0.0.0.0", port=5001, debug=is_debug)
