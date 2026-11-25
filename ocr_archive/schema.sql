-- OCR archive revision ledger.
--
-- Every proofreading operation produces one immutable release. A release stores
-- its body text and its provenance in the same SQLite transaction. Reading an
-- old release therefore never replays edits.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS manuscript (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    NOT NULL UNIQUE,   -- 稿号
    title              TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    current_release_id INTEGER REFERENCES release(id)
);

CREATE TABLE IF NOT EXISTS box (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id INTEGER NOT NULL REFERENCES manuscript(id) ON DELETE CASCADE,
    page_no       INTEGER NOT NULL,               -- 页号，从 1 起
    box_no        INTEGER NOT NULL,               -- 框号，页内阅读顺序
    x             INTEGER NOT NULL,
    y             INTEGER NOT NULL,
    w             INTEGER NOT NULL,
    h             INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    reading_pos   INTEGER NOT NULL,               -- 全文阅读顺序，0 起
    UNIQUE (manuscript_id, page_no, box_no)
);

CREATE TABLE IF NOT EXISTS release (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id INTEGER NOT NULL REFERENCES manuscript(id) ON DELETE CASCADE,
    release_no    INTEGER NOT NULL,               -- 版号（校次），从 1 起
    parent_id     INTEGER REFERENCES release(id),
    body          TEXT    NOT NULL,
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    UNIQUE (manuscript_id, release_no)
);

-- A maximal run of characters that share one source set. body_start / body_end
-- are Unicode code point offsets into release.body, half-open [body_start,
-- body_end).
--
-- Consecutive characters with the same provenance set are stored in one row
-- (one source set). `linear`/`map_start` record the extra fact that the run is
-- still the original OCR text, one character per code point, starting at
-- character `map_start` of its box. That lets a lookup report the exact
-- [start, end) interval inside the box for untouched characters without
-- storing a source set per character. Replacement runs have linear = 0 and
-- inherit their source set unchanged.
CREATE TABLE IF NOT EXISTS body_span (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id    INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    body_start    INTEGER NOT NULL,
    body_end      INTEGER NOT NULL,
    source_set_id INTEGER NOT NULL REFERENCES source_set(id),
    linear        INTEGER NOT NULL DEFAULT 0 CHECK (linear IN (0, 1)),
    map_start     INTEGER,
    UNIQUE (release_id, body_start)
);

-- Provenance sets are content-addressed and shared by every release that uses
-- them. Unchanged characters keep the very same set row even when an edit
-- shifts their body offsets.
CREATE TABLE IF NOT EXISTS source_set (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS source_entry (
    source_set_id INTEGER NOT NULL REFERENCES source_set(id) ON DELETE CASCADE,
    box_id        INTEGER NOT NULL REFERENCES box(id),
    box_start     INTEGER NOT NULL,               -- 框内区间，half-open
    box_end       INTEGER NOT NULL,
    PRIMARY KEY (source_set_id, box_id, box_start, box_end)
);

CREATE INDEX IF NOT EXISTS idx_span_release ON body_span(release_id, body_start);
CREATE INDEX IF NOT EXISTS idx_span_source ON body_span(source_set_id);
CREATE INDEX IF NOT EXISTS idx_entry_set ON source_entry(source_set_id);
CREATE INDEX IF NOT EXISTS idx_release_manuscript ON release(manuscript_id, release_no);
