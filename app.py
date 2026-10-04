import logging
import os

from flask import Flask, jsonify, request

import transport
from escalation import FetchRequest, add_params, with_escalation
from sessions import SessionStore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = Flask(__name__)
store = SessionStore()


@app.route("/api/fetch", methods=["POST"])
def handle_fetch():
    body = request.get_json(silent=True, force=True) or {}
    try:
        req = FetchRequest.from_body(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    result, error = with_escalation(
        store,
        req,
        lambda headers, cookies: transport.fetch(
            req.url,
            headers,
            cookies,
            method=req.method,
            data=req.data,
            follow=req.follow_redirects,
        ),
        tag="fetch",
    )
    if error:
        return jsonify({"error": error}), 502
    return jsonify(result), 200


@app.route("/api/download", methods=["GET"])
def handle_download():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    params = request.args.to_dict()
    params.pop("url", None)
    if params:
        url = add_params(url, params)

    result, error = with_escalation(
        store,
        FetchRequest(url=url),
        lambda headers, cookies: transport.stream(url, headers, cookies),
        target_stealth_ok=lambda ctype: ctype.startswith("text/html"),
        tag="download",
    )
    if error:
        return jsonify({"error": error}), 502
    return result


@app.route("/api/session", methods=["GET"])
def handle_session():
    domain = request.args.get("domain")
    sessions = store.snapshot()
    if domain:
        if domain not in sessions:
            return jsonify({"error": f"No session for {domain}"}), 404
        return jsonify(sessions[domain]), 200
    if not sessions:
        return jsonify({"error": "No sessions available yet"}), 404
    return jsonify(sessions), 200


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        threaded=True,
    )
