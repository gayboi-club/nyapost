import os
import mimetypes
import sqlite3
import time
import functools
import threading
import re
from pathlib import Path

from flask import Flask, render_template, send_file, request, jsonify, abort, redirect, url_for, make_response
from flask_compress import Compress

import config

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["COMPRESS_REGISTER"] = True
app.config["COMPRESS_MIMETYPES"] = ["text/html", "text/css", "application/json"]
Compress(app)


@app.context_processor
def inject_globals():
    return {"site_url": config.FLASK_BASE_URL}

MEMES_DIR = Path(config.MEMES_DIR).resolve()
THUMB_DIR = MEMES_DIR / "_thumbs"


def _in_memes_dir(path):
    return Path(path).resolve().absolute().as_posix().startswith(MEMES_DIR.resolve().absolute().as_posix() + "/")


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )
    return resp

THUMB_SIZE = (400, 280)
CACHE_TTL = 15

_cache = {}
_cache_lock = threading.Lock()


def cached(ttl=CACHE_TTL):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            with _cache_lock:
                entry = _cache.get(key)
                if entry and time.time() - entry["t"] < ttl:
                    return entry["v"]
            r = fn(*args, **kwargs)
            with _cache_lock:
                _cache[key] = {"v": r, "t": time.time()}
            return r
        return wrapper
    return deco


def invalidate_cache():
    with _cache_lock:
        _cache.clear()


@app.template_filter("pluralize")
def pluralize(value):
    if isinstance(value, (list, tuple)):
        value = len(value)
    return "s" if value != 1 else ""


def get_db():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    return conn


def init_db():
    schema = (Path(config.BASE_DIR) / "schema.sql").read_text(encoding="utf-8")
    with get_db() as conn:
        conn.executescript(schema)


@cached(ttl=CACHE_TTL)
def all_memes():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM memes ORDER BY id DESC").fetchall()]


def get_meme(meme_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM memes WHERE id = ?", (meme_id,)).fetchone()
        return dict(row) if row else None


@cached(ttl=CACHE_TTL)
def all_ids():
    with get_db() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM memes ORDER BY id ASC").fetchall()]


def sync_from_disk():
    memes_dir = Path(config.MEMES_DIR)
    if not memes_dir.exists():
        return 0
    synced = 0
    with get_db() as conn:
        existing = {r["filename"] for r in conn.execute("SELECT filename FROM memes").fetchall()}
        for f in memes_dir.iterdir():
            if not f.is_file() or f.name.startswith(".") or f.name == "_thumbs":
                continue
            if f.name in existing:
                continue
            mime_type, _ = mimetypes.guess_type(str(f))
            conn.execute(
                "INSERT INTO memes (filename, original_name, mime_type, file_size) VALUES (?, ?, ?, ?)",
                (f.name, f.name, mime_type or "application/octet-stream", f.stat().st_size),
            )
            synced += 1
        conn.commit()
    if synced:
        invalidate_cache()
    return synced


@app.route("/")
def index():
    return render_template("index.html", memes=all_memes(), all_ids=all_ids())


@app.route("/p/<int:meme_id>")
def meme_page(meme_id):
    meme = get_meme(meme_id)
    if not meme:
        abort(404)
    return render_template("meme.html", meme=meme, all_ids=all_ids())


@app.route("/media/<int:meme_id>")
def media_file(meme_id):
    meme = get_meme(meme_id)
    if not meme:
        abort(404)
    filepath = Path(config.MEMES_DIR) / meme["filename"]
    resolved = filepath.resolve()
    if not resolved.exists() or not resolved.is_file() or not _in_memes_dir(resolved):
        abort(404)
    resp = send_file(str(resolved), mimetype=meme["mime_type"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/thumb/<int:meme_id>")
def thumbnail(meme_id):
    meme = get_meme(meme_id)
    if not meme:
        abort(404)
    thumb = THUMB_DIR / f"{meme_id}.jpg"
    if thumb.exists():
        resp = send_file(str(thumb.resolve()), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    return redirect(url_for("media_file", meme_id=meme_id))


@app.route("/stats")
def stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
        total_size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM memes").fetchone()[0]
        top_uploaders = [
            dict(r) for r in conn.execute(
                "SELECT uploaded_by_name, uploaded_by, COUNT(*) AS count FROM memes WHERE uploaded_by_name != '' GROUP BY uploaded_by_name ORDER BY count DESC LIMIT 20"
            ).fetchall()
        ]
        types = [
            dict(r) for r in conn.execute(
                "SELECT mime_type, COUNT(*) AS count FROM memes GROUP BY mime_type ORDER BY count DESC"
            ).fetchall()
        ]
    return render_template("stats.html", total=total, total_size=total_size, top_uploaders=top_uploaders, types=types)


@app.route("/api/admin/refetch_memes_from_disk", methods=["POST"])
def refetch():
    key = request.headers.get("X-API-Key", "")
    if key != config.FLASK_API_KEY:
        abort(401)
    count = sync_from_disk()
    invalidate_cache()
    return jsonify({"synced": count, "total": len(all_memes())})


if __name__ == "__main__":
    MEMES_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    sync_from_disk()
    print(f"listening on {config.FLASK_HOST}:{config.FLASK_PORT}")
    try:
        from waitress import serve
        serve(app, host=config.FLASK_HOST, port=config.FLASK_PORT)
    except ImportError:
        app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
