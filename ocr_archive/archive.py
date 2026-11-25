from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from flask import current_app, g


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class ArchiveError(Exception):
    """Base class for domain errors that should be shown to the user."""


class ValidationError(ArchiveError):
    pass


class NotFoundError(ArchiveError):
    pass


class StaleReleaseError(ArchiveError):
    def __init__(self, current_release_no: int):
        super().__init__("校订所依据的版本已不是现行版")
        self.current_release_no = current_release_no


@dataclass(frozen=True)
class BoxInput:
    page_no: int
    box_no: int
    x: int
    y: int
    w: int
    h: int
    text: str


@dataclass(frozen=True)
class Span:
    id: int
    start: int
    end: int
    source_set_id: int
    linear: bool = False
    map_start: int | None = None


@dataclass(frozen=True)
class SourceEntry:
    box_id: int
    box_start: int
    box_end: int


@dataclass(frozen=True)
class Release:
    id: int
    manuscript_id: int
    release_no: int
    parent_id: int | None
    body: str
    note: str
    created_at: str


@dataclass(frozen=True)
class Manuscript:
    id: int
    code: str
    title: str
    created_at: str
    current_release_id: int | None


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str | None = None) -> None:
    if path is None:
        path = current_app.config["DATABASE"]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction; roll back completely on any error.

    BEGIN IMMEDIATE takes the writer lock up front, so a concurrent proofing
    request cannot slip between reading the current release and writing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _as_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是整数") from exc
    raise ValidationError(f"{field} 必须是整数")


def validate_text(value: str, label: str) -> str:
    """Reject strings that cannot round-trip as UTF-8 text.

    Python may hold lone surrogates (for example from JSON escapes); those are
    not Unicode scalar values, would be counted as code points but cannot be
    stored in SQLite or rendered as text.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label}包含不成对的 Unicode 代理项") from exc
    if "\x00" in value:
        raise ValidationError(f"{label}不能包含 NUL 字符")
    return value


def parse_box(item: object, index: int) -> BoxInput:
    if not isinstance(item, dict):
        raise ValidationError(f"第 {index + 1} 个识别框必须是 JSON 对象")
    page = item.get("page_no", item.get("page"))
    box = item.get("box_no", item.get("box"))
    if page is None:
        raise ValidationError(f"第 {index + 1} 个识别框缺少 page_no（页号/版号）")
    if box is None:
        raise ValidationError(f"第 {index + 1} 个识别框缺少 box_no（框号）")
    page_no = _as_int(page, f"第 {index + 1} 个框的 page_no")
    box_no = _as_int(box, f"第 {index + 1} 个框的 box_no")
    if page_no < 1:
        raise ValidationError(f"第 {index + 1} 个框的 page_no 必须从 1 起")
    if box_no < 1:
        raise ValidationError(f"第 {index + 1} 个框的 box_no 必须从 1 起")

    rect = item.get("rect")
    if rect is not None:
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValidationError(f"第 {index + 1} 个框的 rect 必须是 [x, y, w, h]")
        x, y, w, h = rect
    else:
        try:
            x = item["x"]
            y = item["y"]
            w = item["w"]
            h = item["h"]
        except KeyError as exc:
            raise ValidationError(f"第 {index + 1} 个框缺少矩形坐标 x/y/w/h") from exc
    x = _as_int(x, f"第 {index + 1} 个框的 x")
    y = _as_int(y, f"第 {index + 1} 个框的 y")
    w = _as_int(w, f"第 {index + 1} 个框的 w")
    h = _as_int(h, f"第 {index + 1} 个框的 h")
    if x < 0 or y < 0:
        raise ValidationError(f"第 {index + 1} 个框的坐标不能为负")
    if w <= 0 or h <= 0:
        raise ValidationError(f"第 {index + 1} 个框的宽高必须大于 0")

    text = item.get("text")
    if not isinstance(text, str):
        raise ValidationError(f"第 {index + 1} 个框缺少文字 text")
    validate_text(text, f"第 {index + 1} 个框的文字")
    return BoxInput(page_no, box_no, x, y, w, h, text)


def parse_boxes(payload: object) -> list[BoxInput]:
    if not isinstance(payload, list):
        raise ValidationError("识别框必须是 JSON 数组，数组顺序即阅读顺序")
    if not payload:
        raise ValidationError("至少需要一个识别框")
    boxes = [parse_box(item, i) for i, item in enumerate(payload)]
    seen: set[tuple[int, int]] = set()
    for box in boxes:
        key = (box.page_no, box.box_no)
        if key in seen:
            raise ValidationError(f"页 {box.page_no} 的框号 {box.box_no} 重复")
        seen.add(key)
    if not "".join(b.text for b in boxes):
        raise ValidationError("所有识别框的文字都为空，无法形成初稿")
    return boxes


def get_manuscript(conn: sqlite3.Connection, code: str) -> Manuscript:
    row = conn.execute(
        "SELECT * FROM manuscript WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"未找到稿号 {code}")
    return Manuscript(
        row["id"],
        row["code"],
        row["title"],
        row["created_at"],
        row["current_release_id"],
    )


def get_release(conn: sqlite3.Connection, manuscript_id: int, release_no: int) -> Release:
    row = conn.execute(
        "SELECT * FROM release WHERE manuscript_id = ? AND release_no = ?",
        (manuscript_id, release_no),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"未找到第 {release_no} 校次")
    return Release(
        row["id"],
        row["manuscript_id"],
        row["release_no"],
        row["parent_id"],
        row["body"],
        row["note"],
        row["created_at"],
    )


def get_release_by_id(conn: sqlite3.Connection, release_id: int) -> Release:
    row = conn.execute("SELECT * FROM release WHERE id = ?", (release_id,)).fetchone()
    if row is None:
        raise NotFoundError("未找到该版本")
    return Release(
        row["id"],
        row["manuscript_id"],
        row["release_no"],
        row["parent_id"],
        row["body"],
        row["note"],
        row["created_at"],
    )


def load_spans(conn: sqlite3.Connection, release_id: int) -> list[Span]:
    rows = conn.execute(
        "SELECT id, body_start, body_end, source_set_id, linear, map_start "
        "FROM body_span WHERE release_id = ? ORDER BY body_start",
        (release_id,),
    ).fetchall()
    return [
        Span(
            r["id"],
            r["body_start"],
            r["body_end"],
            r["source_set_id"],
            bool(r["linear"]),
            r["map_start"],
        )
        for r in rows
    ]


def load_source_entries(
    conn: sqlite3.Connection, source_set_ids: Iterable[int]
) -> dict[int, tuple[SourceEntry, ...]]:
    ids = sorted(set(source_set_ids))
    result: dict[int, list[SourceEntry]] = {i: [] for i in ids}
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT source_set_id, box_id, box_start, box_end FROM source_entry "
        f"WHERE source_set_id IN ({placeholders}) "
        "ORDER BY source_set_id, box_id, box_start, box_end",
        ids,
    ).fetchall()
    for row in rows:
        result[row["source_set_id"]].append(
            SourceEntry(row["box_id"], row["box_start"], row["box_end"])
        )
    return {k: tuple(v) for k, v in result.items()}


def _source_set_key(entries: Sequence[SourceEntry]) -> str:
    canonical = sorted({(e.box_id, e.box_start, e.box_end) for e in entries})
    return json.dumps(canonical, separators=(",", ":"))


def get_or_create_source_set(
    conn: sqlite3.Connection, entries: Iterable[SourceEntry]
) -> int:
    canonical = sorted(
        {(e.box_id, e.box_start, e.box_end) for e in entries}
    )
    if not canonical:
        raise ValidationError("来源集合不能为空")
    key = json.dumps(canonical, separators=(",", ":"))
    row = conn.execute("SELECT id FROM source_set WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO source_set (key) VALUES (?)", (key,))
    set_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO source_entry (source_set_id, box_id, box_start, box_end) "
        "VALUES (?, ?, ?, ?)",
        [(set_id, box_id, a, b) for box_id, a, b in canonical],
    )
    return set_id


def _merge_runs(
    runs: Sequence[tuple[int, int, int, bool, int | None]]
) -> list[tuple[int, int, int, bool, int | None]]:
    """Join adjacent characters that share a provenance set and mapping."""
    merged: list[list] = []
    for start, end, set_id, linear, map_start in runs:
        if start == end:
            continue
        if merged:
            prev = merged[-1]
            same = prev[1] == start and prev[2] == set_id and prev[3] == linear
            if same and linear:
                same = (
                    prev[4] is not None
                    and prev[4] + (prev[1] - prev[0]) == map_start
                )
            if same:
                prev[1] = end
                continue
        merged.append([start, end, set_id, linear, map_start])
    return [(a, b, c, d, e) for a, b, c, d, e in merged]


def ingest(conn: sqlite3.Connection, code: str, title: str, boxes: list[BoxInput]) -> int:
    """Create a manuscript and its first release atomically.

    Returns the manuscript id. Box array order is reading order; the initial
    body is the concatenation of box texts in that order.
    """
    code = code.strip()
    title = title.strip()
    if not code:
        raise ValidationError("稿号不能为空")
    if not title:
        raise ValidationError("标题不能为空")
    validate_text(code, "稿号")
    validate_text(title, "标题")

    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with immediate(conn):
        if conn.execute("SELECT 1 FROM manuscript WHERE code = ?", (code,)).fetchone():
            raise ValidationError(f"稿号 {code} 已存在")
        cur = conn.execute(
            "INSERT INTO manuscript (code, title, created_at) VALUES (?, ?, ?)",
            (code, title, created_at),
        )
        manuscript_id = int(cur.lastrowid)

        body_parts: list[str] = []
        runs: list[tuple[int, int, int, bool, int | None]] = []
        pos = 0
        for reading_pos, box in enumerate(boxes):
            cur = conn.execute(
                "INSERT INTO box "
                "(manuscript_id, page_no, box_no, x, y, w, h, text, reading_pos) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manuscript_id,
                    box.page_no,
                    box.box_no,
                    box.x,
                    box.y,
                    box.w,
                    box.h,
                    box.text,
                    reading_pos,
                ),
            )
            box_id = int(cur.lastrowid)
            if box.text:
                set_id = get_or_create_source_set(
                    conn, [SourceEntry(box_id, 0, len(box.text))]
                )
                runs.append((pos, pos + len(box.text), set_id, True, 0))
                body_parts.append(box.text)
                pos += len(box.text)

        body = "".join(body_parts)
        cur = conn.execute(
            "INSERT INTO release "
            "(manuscript_id, release_no, parent_id, body, note, created_at) "
            "VALUES (?, 1, NULL, ?, ?, ?)",
            (manuscript_id, body, "OCR 初稿", created_at),
        )
        release_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO body_span "
            "(release_id, body_start, body_end, source_set_id, linear, map_start) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (release_id, a, b, c, 1 if d else 0, e)
                for a, b, c, d, e in _merge_runs(runs)
            ],
        )
        conn.execute(
            "UPDATE manuscript SET current_release_id = ? WHERE id = ?",
            (release_id, manuscript_id),
        )
    return manuscript_id


def _selected_source_entries(
    conn: sqlite3.Connection, spans: Sequence[Span], start: int, end: int
) -> set[SourceEntry]:
    """Every original box interval recorded for the selected characters.

    Inheritance uses the stored source records as-is (the whole interval of
    each contributing box), not a freshly sliced sub-interval.
    """
    set_ids = {
        span.source_set_id
        for span in spans
        if span.end > start and span.start < end
    }
    by_set = load_source_entries(conn, set_ids)
    collected: set[SourceEntry] = set()
    for span in spans:
        if span.end <= start or span.start >= end:
            continue
        collected.update(by_set.get(span.source_set_id, ()))
    return collected


def revise(
    conn: sqlite3.Connection,
    manuscript: Manuscript,
    base_release_id: int,
    start: int,
    end: int,
    replacement: str,
    note: str,
) -> Release:
    """Replace or delete one non-empty body range, producing a new release.

    Every introduced code point inherits the union of all box intervals carried
    by every code point in the selected range. Untouched code points keep their
    exact source-set rows, so source records stay shared across releases.
    """
    note = note.strip()
    if not isinstance(replacement, str):
        raise ValidationError("换入文字必须是字符串")
    validate_text(replacement, "换入文字")
    validate_text(note, "校订说明")
    if len(note) > 2000:
        raise ValidationError("校订说明过长")
    if len(replacement) > 200_000:
        raise ValidationError("换入文字过长")

    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with immediate(conn):
        current_row = conn.execute(
            "SELECT current_release_id FROM manuscript WHERE id = ?",
            (manuscript.id,),
        ).fetchone()
        if current_row is None:
            raise NotFoundError("手稿不存在")
        current_id = current_row["current_release_id"]
        base = get_release_by_id(conn, base_release_id)
        if base.manuscript_id != manuscript.id:
            raise NotFoundError("依据版本不属于该手稿")
        if current_id != base_release_id:
            current = get_release_by_id(conn, current_id) if current_id else None
            raise StaleReleaseError(current.release_no if current else 0)

        if not (0 <= start < end <= len(base.body)):
            raise ValidationError(
                f"正文区间必须满足 0 ≤ 起点 < 终点 ≤ {len(base.body)}（码点）"
            )

        spans = load_spans(conn, base.id)
        if not spans:
            raise ValidationError("现行版正文为空，无法校订")

        union = _selected_source_entries(conn, spans, start, end)
        if not union:
            raise ValidationError("选区没有可继承的框内来源")
        union_id = get_or_create_source_set(conn, union)

        new_body = base.body[:start] + replacement + base.body[end:]
        shift = len(replacement) - (end - start)
        new_runs: list[tuple[int, int, int, bool, int | None]] = []

        for span in spans:
            if span.start >= start:
                break
            a, b = span.start, min(span.end, start)
            if a < b:
                new_runs.append((a, b, span.source_set_id, span.linear, span.map_start))

        if replacement:
            # Inserted characters inherit the selection's whole source set; they
            # have no character-by-character mapping back into the box text.
            new_runs.append((start, start + len(replacement), union_id, False, None))

        for span in spans:
            if span.end <= end:
                continue
            a, b = max(span.start, end), span.end
            if a < b:
                map_start = span.map_start
                if span.linear and map_start is not None:
                    map_start += a - span.start
                new_runs.append(
                    (a + shift, b + shift, span.source_set_id, span.linear, map_start)
                )

        merged = _merge_runs(new_runs)
        if merged:
            coverage = sum(b - a for a, b, *_ in merged)
            if coverage != len(new_body):
                raise RuntimeError("内部错误：来源区间未覆盖新正文")
            if merged[0][0] != 0 or merged[-1][1] != len(new_body):
                raise RuntimeError("内部错误：来源区间存在空洞")

        next_no = conn.execute(
            "SELECT COALESCE(MAX(release_no), 0) + 1 AS n FROM release "
            "WHERE manuscript_id = ?",
            (manuscript.id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO release "
            "(manuscript_id, release_no, parent_id, body, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (manuscript.id, next_no, base.id, new_body, note, created_at),
        )
        release_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO body_span "
            "(release_id, body_start, body_end, source_set_id, linear, map_start) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (release_id, a, b, c, 1 if d else 0, e)
                for a, b, c, d, e in merged
            ],
        )
        conn.execute(
            "UPDATE manuscript SET current_release_id = ? WHERE id = ?",
            (release_id, manuscript.id),
        )
        return get_release_by_id(conn, release_id)


def list_releases(conn: sqlite3.Connection, manuscript_id: int) -> list[Release]:
    rows = conn.execute(
        "SELECT * FROM release WHERE manuscript_id = ? ORDER BY release_no",
        (manuscript_id,),
    ).fetchall()
    return [
        Release(
            r["id"],
            r["manuscript_id"],
            r["release_no"],
            r["parent_id"],
            r["body"],
            r["note"],
            r["created_at"],
        )
        for r in rows
    ]


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(set(intervals))
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def lookup_sources(
    conn: sqlite3.Connection,
    release: Release,
    start: int,
    end: int,
    page_no: int | None = None,
) -> list[dict]:
    """Map a body range back to original scan boxes.

    One row per box. Each row reports the page, box number, rectangle, the
    merged intervals inside the box text, and the selected body subranges that
    point at it.

    `exact` is true when every reported interval comes from untouched OCR text
    and is therefore the precise character range selected. Proofreader-inserted
    characters have no character-level mapping and report the inherited whole
    box interval with exact = false.
    """
    if not (0 <= start < end <= len(release.body)):
        raise ValidationError(
            f"查源区间必须满足 0 ≤ 起点 < 终点 ≤ {len(release.body)}（码点）"
        )

    sql = (
        "SELECT bs.id AS span_id, bs.body_start AS span_start, "
        "bs.body_end AS span_end, bs.linear, bs.map_start, "
        "se.box_id, se.box_start, se.box_end, "
        "b.page_no, b.box_no, b.x, b.y, b.w, b.h, b.text, b.reading_pos "
        "FROM body_span bs "
        "JOIN source_entry se ON se.source_set_id = bs.source_set_id "
        "JOIN box b ON b.id = se.box_id "
        "WHERE bs.release_id = ? AND bs.body_end > ? AND bs.body_start < ?"
    )
    params: list[object] = [release.id, start, end]
    if page_no is not None:
        sql += " AND b.page_no = ?"
        params.append(page_no)
    sql += " ORDER BY b.reading_pos, b.page_no, b.box_no"
    rows = conn.execute(sql, params).fetchall()

    boxes: dict[int, dict] = {}
    counted: set[tuple[int, int]] = set()
    for row in rows:
        box_id = row["box_id"]
        item = boxes.get(box_id)
        if item is None:
            item = {
                "box_id": box_id,
                "page_no": row["page_no"],
                "box_no": row["box_no"],
                "rect": {
                    "x": row["x"],
                    "y": row["y"],
                    "w": row["w"],
                    "h": row["h"],
                },
                "box_text": row["text"],
                "intervals": set(),
                "coarse": False,
                "body_ranges": set(),
                "chars": 0,
                "reading_pos": row["reading_pos"],
            }
            boxes[box_id] = item

        span_start = row["span_start"]
        span_end = row["span_end"]
        ov_start = max(span_start, start)
        ov_end = min(span_end, end)
        box_start = row["box_start"]
        box_end = row["box_end"]
        map_start = row["map_start"]
        if bool(row["linear"]) and map_start is not None:
            # Original OCR text: character i of the run maps to box offset
            # map_start + i, so report exactly the stretch selected.
            lo = map_start + (ov_start - span_start)
            hi = map_start + (ov_end - span_start)
            if lo >= box_start and hi <= box_end:
                item["intervals"].add((lo, hi))
            else:
                item["intervals"].add((box_start, box_end))
                item["coarse"] = True
        else:
            # A proofreader-inserted run: report the inherited source interval.
            item["intervals"].add((box_start, box_end))
            item["coarse"] = True

        key = (row["span_id"], box_id)
        if key not in counted:
            counted.add(key)
            item["body_ranges"].add((ov_start, ov_end))
            item["chars"] += ov_end - ov_start

    result = []
    for item in sorted(
        boxes.values(), key=lambda v: (v["reading_pos"], v["page_no"], v["box_no"])
    ):
        item["intervals"] = _merge_intervals(item["intervals"])
        item["body_ranges"] = _merge_intervals(item["body_ranges"])
        item["exact"] = not item["coarse"]
        del item["coarse"]
        result.append(item)
    return result


def body_markup(
    conn: sqlite3.Connection, release: Release, highlight: tuple[int, int] | None = None
) -> str:
    from markupsafe import escape

    spans = load_spans(conn, release.id)
    entries = load_source_entries(conn, {s.source_set_id for s in spans})
    box_ids = {e.box_id for items in entries.values() for e in items}
    box_names: dict[int, tuple[int, int]] = {}
    if box_ids:
        placeholders = ",".join("?" for _ in box_ids)
        for row in conn.execute(
            f"SELECT id, page_no, box_no FROM box WHERE id IN ({placeholders})",
            sorted(box_ids),
        ):
            box_names[row["id"]] = (row["page_no"], row["box_no"])

    parts: list[str] = []
    for span in spans:
        segment = release.body[span.start : span.end]
        span_entries = entries.get(span.source_set_id, ())
        title_bits = []
        for entry in span_entries[:8]:
            page_no, box_no = box_names[entry.box_id]
            title_bits.append(
                f"页{page_no} 框{box_no} [{entry.box_start},{entry.box_end})"
            )
        title = "；".join(title_bits)
        if len(span_entries) > 8:
            title += "；…"
        pieces: list[tuple[str, bool]] = [(segment, False)]
        if highlight and highlight[1] > span.start and highlight[0] < span.end:
            hs = max(highlight[0], span.start)
            he = min(highlight[1], span.end)
            pieced: list[tuple[str, bool]] = []
            if span.start < hs:
                pieced.append((release.body[span.start : hs], False))
            pieced.append((release.body[hs:he], True))
            if he < span.end:
                pieced.append((release.body[he : span.end], False))
            pieces = pieced
        inner = "".join(
            (
                f'<mark class="hit">{escape(text)}</mark>'
                if hit
                else str(escape(text))
            )
            for text, hit in pieces
        )
        parts.append(
            f'<span class="prov" data-start="{span.start}" '
            f'data-end="{span.end}" title="{escape(title)}">{inner}</span>'
        )
    return "".join(parts)
