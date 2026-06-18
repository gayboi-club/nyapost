import os
import json
import base64
import uuid
import hashlib
import hmac
import mimetypes
import sqlite3
import time
import functools
import threading
import re
import secrets
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import mistune
from flask import (
    Flask, render_template, send_file, request,
    jsonify, abort, redirect, url_for, session, make_response,
)
from flask_compress import Compress

import config

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["COMPRESS_REGISTER"] = True
app.config["COMPRESS_MIMETYPES"] = ["text/html", "text/css", "application/json"]
app.config["SESSION_PERMANENT"] = False
Compress(app)

MEMES_DIR = Path(config.MEMES_DIR).resolve()
THUMB_DIR = MEMES_DIR / "_thumbs"
THUMB_SIZE = (400, 280)
CACHE_TTL = 15

_cache = {}
_cache_lock = threading.Lock()


# ── Safe Markdown Renderer ───────────────────────────────────────

class SafeRenderer(mistune.HTMLRenderer):
    def image(self, text, url, title=None):
        return ""

_md = mistune.create_markdown(
    renderer=SafeRenderer(escape=True),
    hard_wrap=True,
    plugins=[mistune.plugins.formatting.strikethrough],
)


def render_markdown(text):
    return _md(text)


# ── Cache ────────────────────────────────────────────────────────

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


# ── DB ───────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema = (Path(config.BASE_DIR) / "schema.sql").read_text(encoding="utf-8")
    with get_db() as conn:
        conn.executescript(schema)


# ── Helpers ──────────────────────────────────────────────────────

def _in_memes_dir(path):
    return Path(path).resolve().absolute().as_posix().startswith(MEMES_DIR.resolve().absolute().as_posix() + "/")


def avatar_url(avatar_hash, discord_id):
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
    idx = (int(discord_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def time_ago(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        diff = now - dt
        secs = int(diff.total_seconds())
        if secs < 60:
            return f"{secs}s ago" if secs else "just now"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h ago"
        days = hrs // 24
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        return f"{months // 12}y ago"
    except Exception:
        return dt_str[:10]


def csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def verify_csrf():
    token = request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not hmac.compare_digest(token, expected):
        abort(403)


# ── Context Processors ───────────────────────────────────────────

@app.context_processor
def inject_globals():
    return {
        "site_url": config.FLASK_BASE_URL,
        "logged_in": "discord_id" in session,
        "current_user": {
            "id": session.get("discord_id"),
            "username": session.get("discord_username"),
            "avatar_hash": session.get("discord_avatar"),
        } if "discord_id" in session else None,
        "csrf_token": csrf_token,
    }


@app.template_global
def avatar(discord_id, avatar_hash, size=32):
    url = avatar_url(avatar_hash, discord_id)
    return f'<img class="avatar" src="{url}" width="{size}" height="{size}" alt="">'


@app.template_filter("pluralize")
def pluralize(value):
    if isinstance(value, (list, tuple)):
        value = len(value)
    return "s" if value != 1 else ""


@app.template_filter("timeago")
def _timeago(value):
    return time_ago(value)


# ── OAUTH2 Routes ────────────────────────────────────────────────

@app.route("/login")
def login():
    next_url = request.args.get("next", "/")
    if next_url.startswith("http"):
        next_url = "/"
    session["_next"] = next_url
    state = csrf_token()
    params = urllib.parse.urlencode({
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    })
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return redirect(url_for("index"))

    if not code or not state:
        abort(400)

    expected = session.get("_csrf_token", "")
    if not expected or not hmac.compare_digest(state, expected):
        abort(403)

    try:
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.DISCORD_REDIRECT_URI,
        }).encode()

        creds = base64.b64encode(f"{config.DISCORD_CLIENT_ID}:{config.DISCORD_CLIENT_SECRET}".encode()).decode()

        req = urllib.request.Request(
            "https://discord.com/api/oauth2/token",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
                "User-Agent": "nyapost/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read())

        access_token = token_data["access_token"]

        req2 = urllib.request.Request(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "nyapost/1.0",
            },
        )
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            user = json.loads(resp2.read())

    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        app.logger.error("oauth2 token exchange failed: %s %s", e.code, body)
        abort(502)
    except Exception as e:
        app.logger.error("oauth2 failed: %s", e, exc_info=True)
        abort(502)

    discord_id = user["id"]
    username = user["username"]
    avatar_hash = user.get("avatar")

    with get_db() as conn:
        conn.execute(
            """INSERT INTO users (discord_id, username, avatar_hash, last_login)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(discord_id) DO UPDATE SET
                 username = excluded.username,
                 avatar_hash = excluded.avatar_hash,
                 last_login = CURRENT_TIMESTAMP""",
            (discord_id, username, avatar_hash),
        )
        conn.commit()

    session["discord_id"] = discord_id
    session["discord_username"] = username
    session["discord_avatar"] = avatar_hash

    next_url = session.pop("_next", "/")
    return redirect(next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Comment Routes ───────────────────────────────────────────────

@app.route("/p/<int:meme_id>/comment", methods=["POST"])
def add_comment(meme_id):
    if "discord_id" not in session:
        abort(401)

    verify_csrf()

    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("meme_page", meme_id=meme_id))

    if len(content) > 10000:
        return redirect(url_for("meme_page", meme_id=meme_id))

    parent_id = request.form.get("parent_id")
    if parent_id:
        try:
            parent_id = int(parent_id)
        except (ValueError, TypeError):
            parent_id = None

    with get_db() as conn:
        conn.execute(
            """INSERT INTO comments (meme_id, user_id, parent_id, content)
               VALUES (?, ?, ?, ?)""",
            (meme_id, session["discord_id"], parent_id, content),
        )
        conn.commit()

    invalidate_cache()
    return redirect(url_for("meme_page", meme_id=meme_id) + f"#comment-{conn.lastrowid}")


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    if "discord_id" not in session:
        abort(401)

    verify_csrf()

    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, meme_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if not row:
            abort(404)
        if row["user_id"] != session["discord_id"]:
            abort(403)

        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        conn.commit()

    invalidate_cache()
    return redirect(url_for("meme_page", meme_id=row["meme_id"]))


# ── Meme Queries ─────────────────────────────────────────────────

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


def get_comments(meme_id):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.*, u.username, u.avatar_hash
               FROM comments c
               JOIN users u ON c.user_id = u.discord_id
               WHERE c.meme_id = ?
               ORDER BY c.parent_id IS NULL ASC, c.created_at DESC""",
            (meme_id,),
        ).fetchall()

    top = []
    by_id = {}
    for r in rows:
        rd = dict(r)
        rd["rendered"] = render_markdown(rd["content"])
        rd["replies"] = []
        by_id[rd["id"]] = rd
        if rd["parent_id"] is None:
            top.append(rd)

    for r in rows:
        rd = dict(r)
        pid = rd["parent_id"]
        if pid is not None and pid in by_id:
            rd["rendered"] = render_markdown(rd["content"])
            by_id[pid]["replies"].append(rd)

    return top


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


# ── Routes ───────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://cdn.discordapp.com; "
        "media-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )
    return resp


@app.route("/")
def index():
    return render_template("index.html", memes=all_memes(), all_ids=all_ids())


@app.route("/p/<int:meme_id>")
def meme_page(meme_id):
    meme = get_meme(meme_id)
    if not meme:
        abort(404)
    comments = get_comments(meme_id)
    return render_template(
        "meme.html",
        meme=meme,
        all_ids=all_ids(),
        comments=comments,
    )


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


# ── Main ─────────────────────────────────────────────────────────

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
