import os
import re
import uuid
import socket
import sqlite3
import mimetypes
import logging
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

intents = discord.Intents.default()
intents.message_content = True


class NyapostBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync(guild=None)
        guild = discord.Object(id=config.DISCORD_GUILD_ID)
        self.tree.add_command(nyapost, guild=guild)
        self.tree.add_command(nyapost_refresh, guild=guild)
        await self.tree.sync(guild=guild)


client = NyapostBot()


def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise ValueError(f"remote server returned {resp.status}")

            content_type = resp.headers.get("Content-Type", "")
            if not any(content_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
                raise ValueError(f"remote Content-Type '{content_type}' not allowed")

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

    cd = resp.headers.get("Content-Disposition", "")
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


# ─── Commands ─────────────────────────────────────────────────────

@app_commands.command(name="nyapost", description="Upload a media file as a nyapost")
@app_commands.guild_only()
@app_commands.describe(file="Upload a file directly", url="Or provide a URL to download from")
async def nyapost(interaction: discord.Interaction, file: discord.Attachment = None, url: str = None):
    if not file and not url:
        await interaction.response.send_message("gimme a file or a url :3c", ephemeral=True)
        return
    if file and url:
        await interaction.response.send_message("only one of file or url, not both :3c", ephemeral=True)
        return

    role = discord.utils.get(interaction.user.roles, id=config.DISCORD_ROLE_ID)
    if role is None:
        await interaction.response.send_message("nuh uh you need the nyaposter role :3c", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        if file:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in config.ALLOWED_EXTENSIONS:
                await interaction.followup.send(
                    f"nooo `{ext}` is banned sorry :3c allowed: {', '.join(config.ALLOWED_EXTENSIONS)}",
                    ephemeral=True,
                )
                return

            if file.size > config.MAX_FILE_SIZE:
                await interaction.followup.send(
                    f"thats too big!! max {config.MAX_FILE_SIZE // 1024 // 1024} MB :3c",
                    ephemeral=True,
                )
                return

            data = await file.read()
            mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
            if not any(mime_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
                await interaction.followup.send("eep!! that file type isnt allowed :3c", ephemeral=True)
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
            await interaction.followup.send("ack!! couldnt save the file :3c", ephemeral=True)
            return

        post_url = f"{config.FLASK_BASE_URL}/p/{meme_id}"
        await interaction.followup.send(
            f"yipeee posted!! {interaction.user.mention} uploaded **{display_name}** as `/p/{meme_id}` {post_url} :3c"
        )

    except ValueError as e:
        await interaction.followup.send(f"eep!! {e} :3c", ephemeral=True)
    except aiohttp.ClientError as e:
        log.error("download failed: %s", e)
        await interaction.followup.send("ack!! couldnt download that url :3c", ephemeral=True)
    except Exception as e:
        log.error("upload failed: %s", e, exc_info=True)
        await interaction.followup.send("ack!! something went wrong :3c", ephemeral=True)


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
                        f"rescanned disk!! synced {data['synced']} new, total {data['total']} nyaposts :3c"
                    )
                else:
                    await interaction.followup.send(f"eep!! flask says {resp.status} :3c")
    except Exception as e:
        log.error("refresh failed: %s", e, exc_info=True)
        await interaction.followup.send("ack!! couldnt reach the site :3c")


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"Serving guild {config.DISCORD_GUILD_ID}")
    print("------")


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set")
        exit(1)
    client.run(config.DISCORD_TOKEN)
