import os
import re
import sqlite3
import mimetypes
import logging
from pathlib import Path
from io import BytesIO

from PIL import Image
import aiohttp
import discord
from discord import app_commands

import config

THUMB_DIR = Path(config.MEMES_DIR) / "_thumbs"
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
        guild = discord.Object(id=config.DISCORD_GUILD_ID)
        self.tree.add_command(nyapost, guild=guild)
        self.tree.add_command(nyapost_refresh, guild=guild)
        await self.tree.sync(guild=guild)


client = NyapostBot()


def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app_commands.command(name="nyapost", description="Upload a media file as a nyapost")
@app_commands.guild_only()
@app_commands.describe(file="The media file to upload as a nyapost")
async def nyapost(interaction: discord.Interaction, file: discord.Attachment):
    role = discord.utils.get(interaction.user.roles, id=config.DISCORD_ROLE_ID)
    if role is None:
        await interaction.response.send_message("nuh uh you need the nyaposter role :3c", ephemeral=True)
        return

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        await interaction.response.send_message(
            f"nooo `{ext}` is banned sorry :3c allowed: {', '.join(config.ALLOWED_EXTENSIONS)}",
            ephemeral=True,
        )
        return

    if file.size > config.MAX_FILE_SIZE:
        await interaction.response.send_message(
            f"thats too big!! max {config.MAX_FILE_SIZE // 1024 // 1024} MB :3c",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)

    try:
        log.info("reading file %s (%d bytes)", file.filename, file.size)
        data = await file.read()
    except Exception as e:
        log.error("failed to read file from discord: %s", e, exc_info=True)
        await interaction.followup.send("eep!! couldnt read the file :3c", ephemeral=True)
        return

    mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
    if not any(mime_type.startswith(p) for p in config.ALLOWED_MIME_PREFIXES):
        log.warning("rejected mime %s from %s", mime_type, interaction.user)
        await interaction.followup.send("eep!! that file type isnt allowed :3c", ephemeral=True)
        return

    clean_name = re.sub(r'[^a-zA-Z0-9._-]', '_', Path(file.filename).name)
    if len(clean_name) > 200:
        clean_name = clean_name[:200]

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO memes (filename, original_name, uploaded_by, uploaded_by_name, mime_type, file_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("pending", clean_name, str(interaction.user.id), interaction.user.name, mime_type, file.size),
        )
        meme_id = cur.lastrowid
        conn.commit()

    final_filename = f"{meme_id}_{clean_name}"
    save_path = Path(config.MEMES_DIR) / final_filename
    save_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        log.info("saving to %s", save_path)
        save_path.write_bytes(data)
        log.info("saved %d bytes OK", len(data))
    except Exception as e:
        log.error("failed to save file: %s", e, exc_info=True)
        with get_db() as conn:
            conn.execute("DELETE FROM memes WHERE id = ?", (meme_id,))
            conn.commit()
        await interaction.followup.send("ack!! couldnt save the file :3c", ephemeral=True)
        return

    with get_db() as conn:
        conn.execute("UPDATE memes SET filename = ? WHERE id = ?", (final_filename, meme_id))
        conn.commit()

    generate_thumbnail(meme_id, save_path, mime_type)

    post_url = f"{config.FLASK_BASE_URL}/p/{meme_id}"
    await interaction.followup.send(
        f"yipeee posted!! {interaction.user.mention} uploaded **{file.filename}** as `/p/{meme_id}` {post_url} :3c"
    )


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


@app_commands.command(name="nyapost-refresh", description="Force rescan memes from disk")
@app_commands.guild_only()
async def nyapost_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": config.FLASK_API_KEY}
            async with session.post(
                f"{config.FLASK_BASE_URL}/api/admin/refetch_memes_from_disk",
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
