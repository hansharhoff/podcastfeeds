"""SQLite persistence via SQLModel."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import DB_PATH


def utcnow() -> datetime:
    return datetime.now(UTC)


class Episode(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source_slug: str = Field(index=True)
    guid: str = Field(index=True)  # dedupe key: entry id / submitted url / digest date
    title: str
    description: str = ""
    link: str = ""
    status: str = Field(default="pending", index=True)  # pending|processing|ready|error
    error: str = ""
    # Either a local file we generated ...
    audio_file: str = ""  # filename inside MEDIA_DIR
    # ... or a remote enclosure passed through from the source feed.
    audio_url: str = ""
    audio_bytes: int = 0
    audio_seconds: int = 0
    voice: str = ""
    image_url: str = ""  # episode artwork (article lead image)
    script: str = ""  # exactly what was spoken: JSON {"blocks":[{"voice","text"}]}
    provenance: str = ""  # JSON: pipeline_version, path, llm backend, scrub info…
    feedback: str = ""  # user feedback lines, newest first
    source_text: str = ""  # full text from the RSS entry — fallback when the page can't be fetched
    created_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = None


class TickTickItem(SQLModel, table=True):
    """A task seen on a watched TickTick list, awaiting Hans' generate/dismiss
    call in the admin queue. task_id is the dedup key — it replaces the old
    watermark, so the pre-existing backlog imports on the first poll."""
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)  # TickTick task id
    project: str = ""  # list name, e.g. "Z Reading"
    title: str = ""
    notes: str = ""  # task content + desc, concatenated
    url: str = ""  # first URL found in title/notes; "" for book references
    kind: str = "article"  # article|pdf|book (heuristic)
    task_created: datetime | None = None  # TickTick createdTime
    first_seen: datetime = Field(default_factory=utcnow)
    status: str = Field(default="queued", index=True)  # queued|generated|dismissed
    episode_id: int | None = None  # set once generated
    last_error: str = ""  # last generate failure, shown inline in the queue
    proposal: str = ""  # actionable suggestion when a generate produced a dud
    # (e.g. book-brief verdict "not-a-book") rather than a plain exception


class KV(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""


_engine = None


def engine():
    global _engine
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(_engine)
        _migrate(_engine)
    return _engine


def _migrate(eng) -> None:
    """Tiny additive migration: create_all doesn't add new columns."""
    from sqlalchemy import text

    with eng.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(episode)"))}
        for col in ("image_url", "script", "provenance", "feedback", "source_text"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE episode ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"))
        # Enforce dedupe at the DB level: drop duplicate (source_slug, guid) rows
        # keeping the lowest id, then a unique index makes concurrent polls safe.
        conn.execute(text(
            "DELETE FROM episode WHERE id NOT IN "
            "(SELECT MIN(id) FROM episode GROUP BY source_slug, guid)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_episode_source_guid "
            "ON episode (source_slug, guid)"
        ))
        tt_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(ticktickitem)"))}
        if "proposal" not in tt_cols:
            conn.execute(text("ALTER TABLE ticktickitem ADD COLUMN proposal TEXT NOT NULL DEFAULT ''"))
        conn.commit()


def session() -> Session:
    return Session(engine())


def kv_get(s: Session, key: str, default: str = "") -> str:
    row = s.get(KV, key)
    return row.value if row else default


def kv_set(s: Session, key: str, value: str) -> None:
    row = s.get(KV, key)
    if row:
        row.value = value
    else:
        row = KV(key=key, value=value)
    s.add(row)
    s.commit()
