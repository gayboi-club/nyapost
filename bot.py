import os
import re
import uuid
import socket
import sqlite3
import mimetypes
import logging
import asyncio
import json
import subprocess
import magic
from pathlib import Path
from urllib.parse import urlparse
from ipaddress import ip_address

from PIL import Image
import aiohttp
import discord
from discord import app_commands

import config

THUMB_DIR = Path(config.MEMES_DIR) / "_thumbs"
INCOMING_DIR = Path(config.MEMES_DIR) / "_incoming"
THUMB_SIZE = (400, 280)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_debug.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nyapost.bot")

COOKIES_FILE = Path(__file__).parent / "instagram_cookies.txt"

async def _resolve_instagram_url(shortcode: str) -> str | None:
    url = f"https://www.instagram.com/p/{shortcode}/"
    cmd = [
        "yt-dlp",
        "--cookies", str(COOKIES_FILE),
        "--dump-json", "--no-download",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning("yt-dlp failed for %s: %s", shortcode, stderr.decode(errors="replace"))
            return None
        data = json.loads(stdout)
        formats = data.get("formats", [])
        best = None
        for f in formats:
            if f.get("vcodec") != "none" and f.get("acodec") != "none":
                if not best or (f.get("tbr") or 0) > (best.get("tbr") or 0):
                    best = f
        if best:
            return best["url"]
        for f in formats:
            if f.get("vcodec") != "none":
                return f["url"]
        return data.get("thumbnail")
    except Exception as e:
        log.warning("instagram resolve failed for %s: %s", shortcode, e)
        return None

intents = discord.Intents.default()
intents.message_content = True


class NyapostBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=config.DISCORD_GUILD_ID)
        self.tree.add_command(nyapost, guild=guild)
        self.tree.add_command(nyapost_refresh, guild=guild)
        self.tree.add_command(nyapost_info, guild=guild)
        self.tree.add_command(nyapost_recent, guild=guild)
        self.tree.add_command(nyapost_my, guild=guild)
        self.tree.add_command(nyapost_random, guild=guild)
        self.tree.add_command(nyapost_search, guild=guild)
        self.tree.add_command(nyapost_stats, guild=guild)
        self.tree.add_command(nyapost_del, guild=guild)
        self.tree.add_command(nyapost_purge, guild=guild)
        self.tree.add_command(nyapost_config, guild=guild)
        self.tree.add_command(nyapost_comment, guild=guild)
        await self.tree.sync(guild=guild)


client = NyapostBot()


def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_config(key):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_config(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO bot_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, value, value),
        )
        conn.commit()


def get_config_all():
    with get_db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM bot_config").fetchall()}


def is_mod(user_id):
    mod_ids = get_config("mod_user_ids") or ""
    return str(user_id) in [uid.strip() for uid in mod_ids.split(",") if uid.strip()]


def generate_thumbnail(meme_id, source_path, mime_type):
    if not mime_type.startswith("image/"):
        return
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.open(source_path)
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(str(THUMB_DIR / f"{meme_id}.jpg"), "JPEG", quality=80)
        log.info("thumbnail generated for %s", meme_id)
    except Exception as e:
        log.warning("thumbnail failed for %s: %s", meme_id, e)


def clean_filename(name):
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', Path(name).name)
    if len(name) > 200:
        name = name[:200]
    return name


def validate_download_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs")
    host = parsed.hostname
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1", "metadata.google.internal"):
        raise ValueError("local addresses not allowed")
    try:
        addrs = socket.getaddrinfo(host, parsed.port or 80)
        for _, _, _, _, sa in addrs:
            addr = ip_address(sa[0])
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                raise ValueError(f"private/reserved IP not allowed: {sa[0]}")
    except ValueError:
        raise
    except Exception:
        pass


async def download_from_url(url):
    validate_download_url(url)

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{uuid.uuid4().hex}.tmp"
    tmp_path = INCOMING_DIR / tmp_name

    hostname = urlparse(url).hostname or ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }

    if "discord" in hostname or "discordapp" in hostname:
        headers["Referer"] = "https://discord.com/"
        headers["Accept"] = "*/*"

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise ValueError(f"remote server returned {resp.status}")

            content_type = resp.headers.get("Content-Type", "")
            if not any(content_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
                raise ValueError(f"remote Content-Type '{content_type}' not allowed")

            cd = resp.headers.get("Content-Disposition", "")

            size = 0
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > config.MAX_FILE_SIZE:
                        tmp_path.unlink(missing_ok=True)
                        raise ValueError(f"file too large (>{config.MAX_FILE_SIZE // 1024 // 1024} MB)")
                    f.write(chunk)

    mime_type = magic.from_file(str(tmp_path), mime=True)
    if not any(mime_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"actual content type '{mime_type}' not allowed")

    fname_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', cd, re.IGNORECASE)
    if fname_match:
        orig_name = fname_match.group(1).strip()
    else:
        url_path = urlparse(url).path.split("/")
        orig_name = url_path[-1] if url_path[-1] else url
    orig_name = clean_filename(orig_name)
    if not orig_name:
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        orig_name = f"download{ext}"

    return tmp_path, orig_name, mime_type, size


async def save_to_db_and_finalize(source_path, orig_name, mime_type, file_size, user_id, user_name):
    clean_name = clean_filename(orig_name)

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO memes (filename, original_name, uploaded_by, uploaded_by_name, mime_type, file_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("pending", clean_name, user_id, user_name, mime_type, file_size),
        )
        meme_id = cur.lastrowid
        conn.commit()

    final_filename = f"{meme_id}_{clean_name}"
    final_path = Path(config.MEMES_DIR) / final_filename
    final_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source_path.replace(final_path)
    except Exception as e:
        log.error("failed to rename %s -> %s: %s", source_path, final_path, e)
        with get_db() as conn:
            conn.execute("DELETE FROM memes WHERE id = ?", (meme_id,))
            conn.commit()
        return None

    with get_db() as conn:
        conn.execute("UPDATE memes SET filename = ? WHERE id = ?", (final_filename, meme_id))
        conn.commit()

    generate_thumbnail(meme_id, final_path, mime_type)
    return meme_id


def delete_meme_files(meme_id, filename):
    file_path = Path(config.MEMES_DIR) / filename
    try:
        file_path.unlink(missing_ok=True)
    except Exception as e:
        log.error("failed to delete file %s: %s", file_path, e)
    thumb = THUMB_DIR / f"{meme_id}.jpg"
    try:
        thumb.unlink(missing_ok=True)
    except Exception:
        pass


# ── Commands ─────────────────────────────────────────────────────

@app_commands.command(name="nyapost", description="Upload a media file as a nyapost")
@app_commands.guild_only()
@app_commands.describe(file="Upload a file directly", url="Or provide a URL to download from")
async def nyapost(interaction: discord.Interaction, file: discord.Attachment = None, url: str = None):
    if not file and not url:
        await interaction.response.send_message("gimme a file or a url", ephemeral=True)
        return
    if file and url:
        await interaction.response.send_message("only one of file or url, not both", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        if file:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in config.ALLOWED_EXTENSIONS:
                await interaction.followup.send(
                    f"nooo `{ext}` is banned sorry allowed: {', '.join(config.ALLOWED_EXTENSIONS)}",
                    ephemeral=True,
                )
                return

            if file.size > config.MAX_FILE_SIZE:
                await interaction.followup.send(
                    f"thats too big!! max {config.MAX_FILE_SIZE // 1024 // 1024} MB",
                    ephemeral=True,
                )
                return

            data = await file.read()
            mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
            if not any(mime_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
                await interaction.followup.send("eep!! that file type isnt allowed", ephemeral=True)
                return

            tmp_path = Path(config.MEMES_DIR) / f"__tmp_{uuid.uuid4().hex}"
            tmp_path.write_bytes(data)

            meme_id = await save_to_db_and_finalize(
                tmp_path, file.filename, mime_type, file.size,
                str(interaction.user.id), interaction.user.name,
            )
            display_name = file.filename
        else:
            tmp_path, orig_name, mime_type, file_size = await download_from_url(url)
            meme_id = await save_to_db_and_finalize(
                tmp_path, orig_name, mime_type, file_size,
                str(interaction.user.id), interaction.user.name,
            )
            display_name = orig_name

        if meme_id is None:
            await interaction.followup.send("ack!! couldnt save the file", ephemeral=True)
            return

        post_url = f"{config.FLASK_BASE_URL}/p/{meme_id}"
        await interaction.followup.send(
            f"yipeee posted!! {interaction.user.mention} uploaded **{display_name}** as `/p/{meme_id}` {post_url}"
        )

    except ValueError as e:
        await interaction.followup.send(f"eep!! {e}", ephemeral=True)
    except aiohttp.ClientError as e:
        log.error("download failed: %s", e)
        await interaction.followup.send("ack!! couldnt download that url", ephemeral=True)
    except Exception as e:
        log.error("upload failed: %s", e, exc_info=True)
        await interaction.followup.send("ack!! something went wrong", ephemeral=True)


@app_commands.command(name="nyapost-refresh", description="Force rescan memes from disk")
@app_commands.guild_only()
async def nyapost_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": config.FLASK_API_KEY}
            async with session.post(
                f"http://127.0.0.1:{config.FLASK_PORT}/api/admin/refetch_memes_from_disk",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await interaction.followup.send(
                        f"rescanned disk!! synced {data['synced']} new, total {data['total']} nyaposts"
                    )
                else:
                    await interaction.followup.send(f"eep!! flask says {resp.status}")
    except Exception as e:
        log.error("refresh failed: %s", e, exc_info=True)
        await interaction.followup.send("ack!! couldnt reach the site")


@app_commands.command(name="nyapost-info", description="Get details about a nyapost by ID")
@app_commands.guild_only()
@app_commands.describe(post_id="The ID of the nyapost")
async def nyapost_info(interaction: discord.Interaction, post_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM memes WHERE id = ?", (post_id,)).fetchone()

    if not row:
        await interaction.response.send_message(f"nyapost #`{post_id}` doesnt exist", ephemeral=True)
        return

    m = dict(row)
    size_str = f"{m['file_size'] / 1024:.1f} KB" if m['file_size'] else "unknown"
    uploader = m['uploaded_by_name'] or "anonymous"
    link = f"{config.FLASK_BASE_URL}/p/{m['id']}"

    embed = discord.Embed(
        title=f"nyapost #{m['id']}",
        description=f"**{m['original_name']}**",
        url=link,
        color=0xa7c080,
    )
    embed.add_field(name="Uploader", value=uploader, inline=True)
    embed.add_field(name="Type", value=m['mime_type'], inline=True)
    embed.add_field(name="Size", value=size_str, inline=True)
    embed.add_field(name="Date", value=m['uploaded_at'][:19], inline=True)
    embed.add_field(name="Link", value=f"[open]({link})", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="nyapost-recent", description="Show the most recent nyaposts")
@app_commands.guild_only()
@app_commands.describe(count="Number of posts to show (1-10, default 5)")
async def nyapost_recent(interaction: discord.Interaction, count: int = 5):
    count = max(1, min(count, 10))

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, original_name, uploaded_by_name, mime_type FROM memes ORDER BY id DESC LIMIT ?",
            (count,),
        ).fetchall()

    if not rows:
        await interaction.response.send_message("no nyaposts yet", ephemeral=True)
        return

    lines = []
    for r in rows:
        icon = "🎥" if r["mime_type"].startswith("video/") else "🖼️"
        uploader = r["uploaded_by_name"] or "anon"
        lines.append(f"{icon} **#{r['id']}** {r['original_name']} ~ {uploader} ~ {config.FLASK_BASE_URL}/p/{r['id']}")

    await interaction.response.send_message("\n".join(lines))


@app_commands.command(name="nyapost-my", description="Show your own recent nyaposts")
@app_commands.guild_only()
async def nyapost_my(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, original_name, uploaded_at, mime_type FROM memes WHERE uploaded_by = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM memes WHERE uploaded_by = ?", (user_id,)).fetchone()[0]

    if not rows:
        await interaction.response.send_message(
            f"u havent uploaded anything yet\nuse `/nyapost` to post something!",
            ephemeral=True,
        )
        return

    lines = [f"**{interaction.user.name}** ~ {total} total nyapost{'s' if total != 1 else ''}"]
    for r in rows:
        icon = "🎥" if r["mime_type"].startswith("video/") else "🖼️"
        lines.append(f"{icon} **#{r['id']}** {r['original_name']} ~ {r['uploaded_at'][:10]} ~ {config.FLASK_BASE_URL}/p/{r['id']}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@app_commands.command(name="nyapost-random", description="Get a random nyapost link")
@app_commands.guild_only()
async def nyapost_random(interaction: discord.Interaction):
    with get_db() as conn:
        row = conn.execute("SELECT id, original_name FROM memes ORDER BY RANDOM() LIMIT 1").fetchone()

    if not row:
        await interaction.response.send_message("no nyaposts yet", ephemeral=True)
        return

    link = f"{config.FLASK_BASE_URL}/p/{row['id']}"
    await interaction.response.send_message(
        f"🎲 random nyapost: **{row['original_name']}** ~ {link}"
    )


@app_commands.command(name="nyapost-search", description="Search nyaposts by filename")
@app_commands.guild_only()
@app_commands.describe(query="Search query (min 2 characters)")
async def nyapost_search(interaction: discord.Interaction, query: str):
    if len(query) < 2:
        await interaction.response.send_message("query too short, need at least 2 chars", ephemeral=True)
        return

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, original_name, uploaded_by_name, mime_type FROM memes WHERE original_name LIKE ? ORDER BY id DESC LIMIT 10",
            (f"%{query}%",),
        ).fetchall()

    if not rows:
        await interaction.response.send_message(f"no results for `{query}`", ephemeral=True)
        return

    lines = [f"search results for `{query}`:"]
    for r in rows:
        icon = "🎥" if r["mime_type"].startswith("video/") else "🖼️"
        uploader = r["uploaded_by_name"] or "anon"
        lines.append(f"{icon} **#{r['id']}** {r['original_name']} ~ {uploader} ~ {config.FLASK_BASE_URL}/p/{r['id']}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@app_commands.command(name="nyapost-stats", description="Show nyapost stats")
@app_commands.guild_only()
async def nyapost_stats(interaction: discord.Interaction):
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
        total_size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM memes").fetchone()[0]
        uploaders = conn.execute("SELECT COUNT(DISTINCT uploaded_by) FROM memes WHERE uploaded_by != ''").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM memes WHERE mime_type LIKE 'image/%'").fetchone()[0]
        videos = conn.execute("SELECT COUNT(*) FROM memes WHERE mime_type LIKE 'video/%'").fetchone()[0]

    embed = discord.Embed(
        title="nyapost stats",
        color=0x7fbbb3,
    )
    embed.add_field(name="Total posts", value=str(total), inline=True)
    embed.add_field(name="Images", value=str(images), inline=True)
    embed.add_field(name="Videos", value=str(videos), inline=True)
    embed.add_field(name="Total size", value=f"{total_size / 1024 / 1024:.1f} MB", inline=True)
    embed.add_field(name="Uploaders", value=str(uploaders), inline=True)

    await interaction.response.send_message(embed=embed)


@app_commands.command(name="nyapost-del", description="Delete a nyapost by ID (mods only)")
@app_commands.guild_only()
@app_commands.describe(post_id="The ID of the nyapost to delete")
async def nyapost_del(interaction: discord.Interaction, post_id: int):
    if not is_mod(interaction.user.id):
        await interaction.response.send_message("u dont have permission for that", ephemeral=True)
        return

    with get_db() as conn:
        row = conn.execute("SELECT * FROM memes WHERE id = ?", (post_id,)).fetchone()

    if not row:
        await interaction.response.send_message(f"nyapost #`{post_id}` doesnt exist", ephemeral=True)
        return

    m = dict(row)
    delete_meme_files(m["id"], m["filename"])

    with get_db() as conn:
        conn.execute("DELETE FROM memes WHERE id = ?", (post_id,))
        conn.commit()

    await interaction.response.send_message(
        f"deleted nyapost #`{post_id}` (**{m['original_name']}**) by {m['uploaded_by_name'] or 'unknown'}",
        ephemeral=True,
    )
    log.info("mod %s deleted nyapost %s (%s)", interaction.user.id, post_id, m["filename"])


@app_commands.command(name="nyapost-purge", description="Delete all nyaposts by a user (mods only)")
@app_commands.guild_only()
@app_commands.describe(user_id="The Discord user ID whose posts to delete")
async def nyapost_purge(interaction: discord.Interaction, user_id: str):
    if not is_mod(interaction.user.id):
        await interaction.response.send_message("u dont have permission for that", ephemeral=True)
        return

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, filename, original_name FROM memes WHERE uploaded_by = ?", (user_id,)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(f"no nyaposts found for user `{user_id}`", ephemeral=True)
        return

    for r in rows:
        delete_meme_files(r["id"], r["filename"])

    with get_db() as conn:
        conn.execute("DELETE FROM memes WHERE uploaded_by = ?", (user_id,))
        conn.commit()

    await interaction.response.send_message(
        f"purged {len(rows)} nyapost{'s' if len(rows) != 1 else ''} by user `{user_id}`",
        ephemeral=True,
    )
    log.info("mod %s purged %s posts by user %s", interaction.user.id, len(rows), user_id)


@app_commands.command(name="nyapost-config", description="View or set bot config (mods only)")
@app_commands.guild_only()
@app_commands.describe(key="Config key to view or set", value="Value to set (omit to view current)")
async def nyapost_config(interaction: discord.Interaction, key: str = None, value: str = None):
    if not is_mod(interaction.user.id):
        await interaction.response.send_message("u dont have permission for that", ephemeral=True)
        return

    if key is None:
        all_cfg = get_config_all()
        if not all_cfg:
            await interaction.response.send_message("no config values set", ephemeral=True)
            return
        lines = ["**bot config:**"]
        for k, v in all_cfg.items():
            display = v if len(v) < 60 else v[:57] + "..."
            lines.append(f"`{k}` = {display}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    if value is None:
        current = get_config(key)
        if current is None:
            await interaction.response.send_message(f"config key `{key}` not set", ephemeral=True)
        else:
            await interaction.response.send_message(f"`{key}` = {current}", ephemeral=True)
        return

    set_config(key, value)
    await interaction.response.send_message(f"set `{key}` = {value}", ephemeral=True)
    log.info("mod %s set config %s = %s", interaction.user.id, key, value)


@app_commands.command(name="nyapost-comment", description="Leave a comment on a nyapost")
@app_commands.guild_only()
@app_commands.describe(post_id="The ID of the nyapost", content="Your comment text")
async def nyapost_comment(interaction: discord.Interaction, post_id: int, content: str):
    if len(content) > 10000:
        await interaction.response.send_message("comment too long (max 10000 chars)", ephemeral=True)
        return

    with get_db() as conn:
        row = conn.execute("SELECT id FROM memes WHERE id = ?", (post_id,)).fetchone()

    if not row:
        await interaction.response.send_message(f"nyapost #`{post_id}` doesnt exist", ephemeral=True)
        return

    with get_db() as conn:
        conn.execute(
            "INSERT INTO comments (meme_id, user_id, content) VALUES (?, ?, ?)",
            (post_id, str(interaction.user.id), content),
        )
        conn.commit()

    link = f"{config.FLASK_BASE_URL}/p/{post_id}"
    await interaction.response.send_message(
        f"commented on nyapost #`{post_id}` ~ {link}", ephemeral=True
    )


# ── Events ───────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"Serving guild {config.DISCORD_GUILD_ID}")
    print("------")


@client.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = get_config("auto_upload_channel")
    if not channel_id or str(message.channel.id) != channel_id:
        return

    raw_urls = re.findall(r'https?://[^\s<>"\'\[\]]+', message.content) if message.content else []
    if not message.attachments and not raw_urls:
        return

    await message.add_reaction("⏳")

    results = []
    all_ok = True

    KNOWN_MEDIA_HOSTS = {"giphy.com", "media.giphy.com", "gfycat.com", "tenor.com", "media.tenor.com",
                         "imgur.com", "i.imgur.com", "cdn.discordapp.com", "media.discordapp.net",
                         "cdninstagram.com"}
    INSTA_DOMAINS = {"instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am",
                     "kkinstagram.com", "www.kkinstagram.com"}
    INSTA_SHORTCODE_RE = re.compile(r"/(?:p|reel|tv|share)/([A-Za-z0-9_-]+)")
    TENOR_EMBED_RE = re.compile(r'<meta[^>]*\s+property="og:(?:video(?::secure_url|:url)?|image)"\s+content="([^"]+)"', re.IGNORECASE)

    for attachment in message.attachments:
        ext = os.path.splitext(attachment.filename)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            all_ok = False
            log.info("auto-upload skipped %s: bad extension %s", attachment.filename, ext)
            continue

        if attachment.size > config.MAX_FILE_SIZE:
            all_ok = False
            log.info("auto-upload skipped %s: too large", attachment.filename)
            continue

        mime_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
        if not any(mime_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
            all_ok = False
            log.info("auto-upload skipped %s: bad mime %s", attachment.filename, mime_type)
            continue

        try:
            data = await attachment.read()
            tmp_path = Path(config.MEMES_DIR) / f"__tmp_{uuid.uuid4().hex}"
            tmp_path.write_bytes(data)

            meme_id = await save_to_db_and_finalize(
                tmp_path, attachment.filename, mime_type, attachment.size,
                str(message.author.id), message.author.name,
            )

            if meme_id:
                results.append(f"/p/{meme_id}")
                log.info("auto-uploaded %s as /p/%s", attachment.filename, meme_id)
            else:
                all_ok = False
        except Exception as e:
            log.error("auto-upload attachment failed: %s", e, exc_info=True)
            all_ok = False

    for raw_url in raw_urls:
        try:
            parsed = urlparse(raw_url)
            hostname = parsed.hostname or ""
            path = parsed.path or ""

            is_insta = any(hostname.endswith(d) for d in INSTA_DOMAINS)
            if is_insta:
                m = INSTA_SHORTCODE_RE.search(path)
                if m:
                    sc = m.group(1)
                    media_url = await _resolve_instagram_url(sc)
                    if media_url:
                        raw_url = media_url
                        log.info("resolved instagram url to %s", raw_url)
                        parsed = urlparse(raw_url)
                        hostname = parsed.hostname or ""
                        path = parsed.path or ""
                    else:
                        log.info("instagram resolve failed for %s", raw_url)
                        all_ok = False
                        continue
                else:
                    log.info("no shortcode in instagram url: %s", raw_url)
                    all_ok = False
                    continue

            is_known_host = any(hostname.endswith(d) for d in KNOWN_MEDIA_HOSTS)
            is_media_ext = os.path.splitext(path)[1].lower() in config.ALLOWED_EXTENSIONS

            if not is_known_host and not is_media_ext:
                log.info("auto-upload skipped url %s: not a recognizable media link", raw_url)
                all_ok = False
                continue

            if hostname in ("tenor.com", "www.tenor.com"):
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(raw_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            html = await resp.text()
                    m = TENOR_EMBED_RE.search(html)
                    if m:
                        raw_url = m.group(1)
                        log.info("resolved tenor url to %s", raw_url)
                except Exception as e:
                    log.info("failed to resolve tenor url %s: %s", raw_url, e)

            done = False
            if "discord" in hostname and "/attachments/" in path:
                parts = path.split("/")
                if len(parts) >= 5 and parts[1] == "attachments":
                    c_id = int(parts[2])
                    m_id = int(parts[3])
                    chan = client.get_channel(c_id)
                    if chan:
                        try:
                            msg = await chan.fetch_message(m_id)
                            for att in msg.attachments:
                                ext = os.path.splitext(att.filename)[1].lower()
                                if ext in config.ALLOWED_EXTENSIONS and att.size <= config.MAX_FILE_SIZE:
                                    data = await att.read()
                                    tmp = Path(config.MEMES_DIR) / f"__tmp_{uuid.uuid4().hex}"
                                    tmp.write_bytes(data)
                                    mime_type = att.content_type or "application/octet-stream"
                                    mid = await save_to_db_and_finalize(
                                        tmp, att.filename, mime_type, att.size,
                                        str(message.author.id), message.author.name,
                                    )
                                    if mid:
                                        results.append(f"/p/{mid}")
                                        log.info("auto-uploaded discord attachment %s as /p/%s", att.filename, mid)
                                        done = True
                                        break
                        except Exception:
                            log.info("discord fetch failed for %s", raw_url)

            if done:
                continue

            tmp_path, orig_name, mime_type, file_size = await download_from_url(raw_url)
            meme_id = await save_to_db_and_finalize(
                tmp_path, orig_name, mime_type, file_size,
                str(message.author.id), message.author.name,
            )
            if meme_id:
                results.append(f"/p/{meme_id}")
                log.info("auto-uploaded url %s as /p/%s", raw_url, meme_id)
            else:
                all_ok = False
        except Exception as e:
            log.info("auto-upload url failed %s: %s", raw_url, e)
            all_ok = False

    try:
        await message.remove_reaction("⏳", client.user)
    except Exception:
        pass

    if results:
        await message.add_reaction("✅")
    else:
        await message.add_reaction("❌")


# ── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set")
        exit(1)
    client.run(config.DISCORD_TOKEN)
