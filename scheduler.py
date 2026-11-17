#!/usr/bin/env python3
"""
Scheduler module.

Runs product collectors from scripts/ every N minutes. After a cycle it
notifies about newly discovered products and refreshes the live catalog.
No JSON files involved.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import db
import events

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = (BASE_DIR / "scripts").resolve()
LOGS_DIR = (BASE_DIR / "logs").resolve()

SCRIPT_TIMEOUT = 120
REFRESH_SECONDS = 10 * 60
_cycle_lock = threading.Lock()
_active_lock = threading.Lock()
_active_cancel_event = None
_active_process = None


def create_cancel_event():
    """Create and register a cancellation token before a cycle thread starts."""
    global _active_cancel_event
    cancel_event = threading.Event()
    with _active_lock:
        _active_cancel_event = cancel_event
    return cancel_event


def request_stop(cancel_event=None):
    """Ask the active cycle to stop and terminate its current collector."""
    with _active_lock:
        target_event = cancel_event or _active_cancel_event
        process = _active_process
    if target_event is None or target_event.is_set():
        return False
    target_event.set()
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    return True

# ---------- Notifications ----------

def _send_one(title, message, link=None, group=None, notif_id=None):
    """Send a single notification via termux-notification (best effort)."""
    cmd = [
        "termux-notification",
        "--title", str(title),
        "--content", str(message),
        "--priority", "high",
    ]
    if link:
        # The argument is a shell command, so quote the URL safely.
        cmd += ["--action", f"termux-open-url {shlex.quote(link)}"]
    if group:
        cmd += ["--group", str(group)]
    if notif_id:
        cmd += ["--id", str(notif_id)]

    try:
        subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - notifications must never crash a cycle
        print(f"[scheduler] notification failed: {exc}")


def notify_new_items(added):
    """Send one Nintendo notification for newly discovered products."""
    if not added:
        return
    titles = " | ".join(item.get("title", "") for item in added[:3])
    more = f" (+{len(added) - 3})" if len(added) > 3 else ""
    _send_one(
        title=f"{len(added)} new Nintendo game(s)",
        message=titles + more,
        link=added[0].get("link") or None,
        group="Nintendo",
        notif_id="nintendo-catalog",
    )


# ---------- Script execution ----------

def _iter_source_scripts():
    """
    Yield collector scripts directly from scripts/ in a stable order.
    """
    pattern = str(SCRIPTS_DIR / "*.py")
    for path in sorted(glob.glob(pattern)):
        if os.path.basename(path).startswith("_"):
            continue
        yield path


def available_sources():
    """Return the collector scripts available for the UI source picker."""
    labels = {
        "dreamkala": "DreamKala", "igamer": "iGamer", "nakhlmarket": "NakhlMarket",
        "progame": "ProGame", "psparsi": "PSParsi", "pspro": "PSPro",
        "rosegamers": "RoseGamers", "techsiro": "TechSiro",
        "westgamestore": "WestGameStore",
    }
    return [{"id": Path(path).stem, "label": labels.get(Path(path).stem, Path(path).stem)}
            for path in _iter_source_scripts()]


def enabled_sources():
    """Read the user's selected collectors; default to all available sites."""
    all_sources = [source["id"] for source in available_sources()]
    raw = db.get_setting("enabled_sources")
    if not raw:
        return all_sources
    try:
        selected = json.loads(raw)
    except (TypeError, ValueError):
        return all_sources
    if not isinstance(selected, list):
        return all_sources
    selected = [name for name in all_sources if name in selected]
    return selected or all_sources


def run_script(script_path, log_dir, cancel_event=None):
    """Run one collector and return its captured output and result."""
    global _active_process
    name = os.path.basename(script_path)
    log_file = os.path.join(log_dir, f"{name}.log")

    def write_log(text):
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(text)

    process = None
    try:
        if cancel_event and cancel_event.is_set():
            return {"success": False, "cancelled": True, "returncode": None,
                    "stdout": "", "stderr": "", "issues": ["Stopped by user"]}
        process = subprocess.Popen(
            [sys.executable, script_path], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(SCRIPTS_DIR),
        )
        with _active_lock:
            _active_process = process
        deadline = time.monotonic() + SCRIPT_TIMEOUT
        while True:
            if cancel_event and cancel_event.is_set():
                if process.poll() is None:
                    process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                write_log(f"\n[{datetime.now().isoformat()}] STOPPED by user; rc={process.returncode}\n")
                if stdout:
                    write_log("STDOUT:\n" + stdout + "\n")
                if stderr:
                    write_log("STDERR:\n" + stderr + "\n")
                return {"success": False, "cancelled": True, "returncode": process.returncode,
                        "stdout": stdout or "", "stderr": stderr or "",
                        "issues": ["Stopped by user"]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                stdout, stderr = process.communicate()
                write_log(f"\n[{datetime.now().isoformat()}] TIMEOUT after {SCRIPT_TIMEOUT}s\n")
                if stdout:
                    write_log("STDOUT:\n" + stdout + "\n")
                if stderr:
                    write_log("STDERR:\n" + stderr + "\n")
                return {"success": False, "returncode": None, "stdout": stdout or "",
                        "stderr": stderr or "", "issues": [f"Timed out after {SCRIPT_TIMEOUT}s"]}
            try:
                stdout, stderr = process.communicate(timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        write_log(f"\n[{datetime.now().isoformat()}] rc={process.returncode}\n")
        if stdout:
            write_log("STDOUT:\n" + stdout + "\n")
        if stderr:
            write_log("STDERR:\n" + stderr + "\n")
        output = "\n".join(part for part in (stdout, stderr) if part)
        issue_lines = [line for line in output.splitlines()
                       if re.search(r"\b(error|failed|failure|traceback|exception)\b|خطا|ناموفق|no games collected|no nintendo switch games found", line, re.I)]
        return {"success": process.returncode == 0 and not issue_lines,
                "returncode": process.returncode, "stdout": stdout or "",
                "stderr": stderr or "", "issues": issue_lines}
    except Exception as exc:  # noqa: BLE001 - isolate failures per script
        write_log(f"\n[{datetime.now().isoformat()}] ERROR: {exc}\n")
        return {"success": False, "returncode": None, "stdout": "",
                "stderr": str(exc), "issues": [str(exc)]}
    finally:
        with _active_lock:
            if _active_process is process:
                _active_process = None


# ---------- Cycle ----------

def cycle_once(on_progress=None, trigger="automatic", cancel_event=None):
    """Serialize refresh cycles so manual and scheduled runs cannot overlap."""
    global _active_cancel_event
    if cancel_event is None:
        cancel_event = threading.Event()
    with _cycle_lock:
        with _active_lock:
            _active_cancel_event = cancel_event
        try:
            return _run_cycle(on_progress=on_progress, trigger=trigger,
                              cancel_event=cancel_event)
        finally:
            with _active_lock:
                if _active_cancel_event is cancel_event:
                    _active_cancel_event = None


def _run_cycle(on_progress=None, trigger="automatic", cancel_event=None):
    """
    Run a single cycle:
      1. baseline = db.max_id()
      2. run every collector in scripts/
      3. added = db.items_since(baseline)
      4. notify about new products and refresh the live catalog
    """
    db.init_db()
    os.makedirs(str(LOGS_DIR), exist_ok=True)

    before = {item["id"] for item in db.get_items()}

    selected = set(enabled_sources())
    source_scripts = [path for path in _iter_source_scripts()
                      if Path(path).stem in selected]

    total = len(source_scripts)
    run_log = {"trigger": trigger,
               "started_at": datetime.now().astimezone().isoformat(),
               "finished_at": None, "status": "running", "total": total,
               "selected_sources": sorted(selected), "sites": []}

    def save_run_log():
        db.set_setting("latest_update_log", json.dumps(run_log, ensure_ascii=False))

    def progress(data):
        data["trigger"] = trigger
        if on_progress:
            on_progress(data)

    save_run_log()
    for index, script_path in enumerate(source_scripts):
        name = os.path.splitext(os.path.basename(script_path))[0]
        site_log = {"name": name, "status": "running",
                    "started_at": datetime.now().astimezone().isoformat(),
                    "finished_at": None, "returncode": None,
                    "stdout": "", "stderr": "", "issues": []}
        run_log["sites"].append(site_log)
        save_run_log()
        progress({"state": "running", "script": name,
                  "percent": round(index * 100 / total) if total else 100,
                  "completed": index, "total": total})
        result = run_script(script_path, str(LOGS_DIR), cancel_event)
        site_log.update(result)
        site_log["status"] = ("cancelled" if result.get("cancelled") else
                               "success" if result["success"] else "failure")
        site_log["finished_at"] = datetime.now().astimezone().isoformat()
        save_run_log()
        if result.get("cancelled"):
            progress({"state": "stopped", "script": name,
                      "percent": round(index * 100 / total) if total else 0,
                      "completed": index, "total": total})
            break
        progress({"state": "running" if index + 1 < total else "finishing",
                  "script": name,
                  "percent": round((index + 1) * 100 / total) if total else 100,
                  "completed": index + 1, "total": total,
                  "success": result["success"]})

    current = db.get_items()
    added = [item for item in current if item["id"] not in before]

    if added:
        notify_new_items(added)

    events.broadcast("catalog_refreshed", {"new_count": len(added), "items_count": db.count()})

    run_log["finished_at"] = datetime.now().astimezone().isoformat()
    if cancel_event and cancel_event.is_set():
        run_log["status"] = "cancelled"
    else:
        run_log["status"] = "success" if all(site["status"] == "success" for site in run_log["sites"]) else "failure"
    save_run_log()
    completed_count = sum(site["status"] != "running" for site in run_log["sites"])
    progress({"state": "stopped" if run_log["status"] == "cancelled" else "done",
              "script": "", "percent": round(len([s for s in run_log["sites"] if s["status"] == "success"]) * 100 / total) if total else 100,
              "completed": completed_count, "total": total,
              "success": run_log["status"] == "success"})

    return added


# ---------- Background loop ----------

def start_scheduler():
    """Start the scheduler loop in a background daemon thread."""
    def loop():
        while True:
            try:
                added = cycle_once(
                    on_progress=lambda data: events.broadcast("run_progress", data),
                    trigger="automatic",
                )
                print(
                    f"[scheduler] {datetime.now().strftime('%H:%M:%S')} "
                    f"cycle done | new items: {len(added)}"
                )
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"[scheduler] error: {exc}")

            time.sleep(REFRESH_SECONDS)

    thread = threading.Thread(target=loop, daemon=True, name="scheduler")
    thread.start()
    return thread


if __name__ == "__main__":
    db.init_db()
    added_items = cycle_once()
    print(f"[scheduler] single cycle finished | new items: {len(added_items)}")
