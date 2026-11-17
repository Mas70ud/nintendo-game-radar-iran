#!/usr/bin/env python3
"""
Flask web server + JSON API.

All state lives in SQLite (see db.py). No JSON files, no external services.
The scheduler runs in a background thread inside this process.
"""
import os
import queue
import json
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import db
import events
import image_cache
from scheduler import (
    available_sources,
    cycle_once,
    create_cancel_event,
    enabled_sources,
    request_stop,
)

# CORS origins. Default "*" preserves the original behaviour; set the
# NOTIFIER_CORS_ORIGINS environment variable (comma separated) to lock it down.
_cors_env = os.environ.get("NOTIFIER_CORS_ORIGINS", "*").strip()
CORS_ORIGINS = "*" if _cors_env == "*" else [o.strip() for o in _cors_env.split(",") if o.strip()]

app = Flask(__name__)
_manual_run_lock = threading.Lock()
_manual_run_active = False
_manual_cancel_event = None

try:  # CORS is optional: the app works fine same-origin without flask-cors.
    from flask_cors import CORS

    CORS(app, origins=CORS_ORIGINS, supports_credentials=True)
except ImportError:  # pragma: no cover - depends on the environment
    print("[app] flask-cors not installed; CORS disabled")


# ---------- Auth ----------

def auth_enabled():
    """Auth is active only when a non-empty password is configured."""
    return bool(db.get_setting("password") or "")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not auth_enabled():
            return view(*args, **kwargs)
        if session.get("authed"):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == (db.get_setting("password") or ""):
            session["authed"] = True
            session.permanent = True
            target = request.args.get("next") or url_for("index")
            # Only allow same-site relative redirects. "//evil.com" is a
            # protocol-relative URL and must be rejected too.
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("index")
            return redirect(target)
        error = "Wrong password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Pages ----------

@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ---------- Timeline API ----------

@app.route("/api/timeline", methods=["GET"])
@login_required
def api_timeline():
    limit = request.args.get("limit", type=int)
    query = request.args.get("q", type=str)
    items = db.get_items(
        limit=limit,
        search=query,
        sites=enabled_sources(),
    )
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.route("/api/sources", methods=["GET", "POST"])
@login_required
def api_sources():
    sources = available_sources()
    valid_ids = {source["id"] for source in sources}
    if request.method == "GET":
        return jsonify({"ok": True, "sources": sources,
                        "selected": enabled_sources()})

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid website selection."}), 400
    selected = payload.get("sources")
    if not isinstance(selected, list):
        return jsonify({"ok": False, "error": "Choose one or more websites."}), 400
    selected = list(dict.fromkeys(name for name in selected
                                  if isinstance(name, str) and name in valid_ids))
    if not selected:
        return jsonify({"ok": False, "error": "Choose at least one website."}), 400
    db.set_setting("enabled_sources", json.dumps(selected, ensure_ascii=False))
    return jsonify({"ok": True, "selected": selected})


@app.route("/api/run", methods=["POST"])
@login_required
def api_run_now():
    global _manual_run_active
    global _manual_cancel_event
    with _manual_run_lock:
        if _manual_run_active:
            return jsonify({"ok": False, "error": "Update already running"}), 409
        _manual_run_active = True
        _manual_cancel_event = create_cancel_event()
        cancel_event = _manual_cancel_event

    def report(data):
        events.broadcast("run_progress", data)

    def run_cycle():
        global _manual_run_active
        global _manual_cancel_event
        try:
            cycle_once(on_progress=report, trigger="manual", cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001 - report failure to the client
            raw = db.get_setting("latest_update_log")
            try:
                log = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                log = {}
            log.update({"status": "failure", "finished_at": datetime.now().astimezone().isoformat(), "error": str(exc)})
            db.set_setting("latest_update_log", json.dumps(log, ensure_ascii=False))
            report({"state": "error", "error": str(exc)})
        finally:
            with _manual_run_lock:
                _manual_run_active = False
                _manual_cancel_event = None

    threading.Thread(target=run_cycle, daemon=True, name="manual-catalog-refresh").start()
    return jsonify({"ok": True, "started": True}), 202


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop_now():
    with _manual_run_lock:
        if not _manual_run_active or _manual_cancel_event is None:
            return jsonify({"ok": False, "error": "No update is currently running."}), 409
        cancel_event = _manual_cancel_event
    request_stop(cancel_event)
    return jsonify({"ok": True, "stopping": True}), 202


@app.route("/api/run-log", methods=["GET"])
@login_required
def api_run_log():
    raw = db.get_setting("latest_update_log")
    if not raw:
        return jsonify({"ok": True, "log": None})
    try:
        return jsonify({"ok": True, "log": json.loads(raw)})
    except (TypeError, ValueError):
        return jsonify({"ok": True, "log": None})


@app.route("/api/image", methods=["GET"])
@login_required
def api_image():
    source_url = request.args.get("url", "")
    if not db.is_catalog_image(source_url):
        abort(404)
    try:
        path, mime = image_cache.get_cached_image(source_url)
    except Exception as exc:  # noqa: BLE001 - a broken remote thumbnail should not stop the catalog
        return jsonify({"ok": False, "error": str(exc)}), 502
    response = send_file(path, mimetype=mime, conditional=True, max_age=31_536_000)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/health", methods=["GET"])
def api_health():
    """Unauthenticated liveness probe (no sensitive data)."""
    return jsonify(
        {
            "ok": True,
            "items": db.count(),
            "subscribers": events.subscriber_count(),
        }
    )


# ---------- SSE ----------

@app.route("/api/events")
@login_required
def api_events():
    def stream():
        subscriber = events.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = subscriber.get(timeout=25)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                import json  # local import: SSE wire format only

                yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe(subscriber)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)


# ---------- Entry ----------

def _open_browser(url):
    """Best-effort: open the web UI in the default browser."""
    import threading
    import webbrowser

    def opener():
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - opening the browser is optional
            print(f"[app] could not open browser: {exc}")

    # Give the server a moment to bind before the browser connects.
    threading.Timer(1.0, opener).start()


def main():
    db.init_db()
    db.ensure_secret_key()

    app.secret_key = db.get_setting("secret_key")

    host = os.environ.get("NOTIFIER_HOST", "0.0.0.0")
    port = int(os.environ.get("NOTIFIER_PORT", "5050"))
    print(f"[nintendo-notifier] starting Flask server on http://{host}:{port}")

    # Auto-open the browser unless explicitly disabled (NOTIFIER_OPEN_BROWSER=0).
    open_browser = os.environ.get("NOTIFIER_OPEN_BROWSER", "1").strip().lower()
    if open_browser not in ("0", "false", "no", "off"):
        browser_host = "localhost" if host in ("0.0.0.0", "::") else host
        _open_browser(f"http://{browser_host}:{port}/")

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
