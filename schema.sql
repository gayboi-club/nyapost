CREATE TABLE IF NOT EXISTS memes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    uploaded_by     TEXT NOT NULL DEFAULT '',
    uploaded_by_name TEXT NOT NULL DEFAULT '',
    uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    mime_type       TEXT NOT NULL DEFAULT 'application/octet-stream',
    file_size       INTEGER DEFAULT 0,
    width           INTEGER DEFAULT 0,
    height          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    discord_id  TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    avatar_hash TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meme_id    INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
