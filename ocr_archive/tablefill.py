"""统计表补字：按纸面行列合计求解问号，并在唯一解时一次发版。

档案员在现行稿件页录入：

* 2～4 行、2～4 列数据格，每格给出正文中的码点区间（半开、互不重叠）；
* 纸面上仍然清楚的行合计与列合计。

每格正文只含 ``0``～``9`` 与半角 ``?``，每格至多一个问号，全表 1～6 个。
每个问号补一位数字后，整表必须同时满足全部行合计与列合计。

求解结果分三种：

* ``unique``：唯一解，调用方在同一写事务内补齐全部问号、生成新版，并把
  供数用的 CSV 规格随新版入库；每个新数字继承对应问号所在正文段的来源；
* ``none``：无解，不发版；
* ``multiple``：多解，只回前两组填法供复核，不发版。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import archive
from .archive import (
    NotFoundError,
    StaleReleaseError,
    ValidationError,
    get_or_create_source_set,
    get_release_by_id,
    immediate,
    load_spans,
    load_source_entries,
)

MIN_DIM = 2
MAX_DIM = 4
MAX_QUESTIONS = 6
MAX_SOLUTIONS_RETURNED = 2
CELL_CHARS = frozenset("0123456789?")


@dataclass(frozen=True)
class TableSpec:
    rows: int
    cols: int
    # cells[r][c] = (body_start, body_end)，半开码点区间
    cells: tuple[tuple[tuple[int, int], ...], ...]
    row_totals: tuple[int, ...]
    col_totals: tuple[int, ...]


@dataclass(frozen=True)
class FillResult:
    status: str  # "unique" | "none" | "multiple"
    # 完整数据格（已替换问号），按原表行列顺序；none 时为空，multiple 至多两组
    solutions: tuple[tuple[tuple[str, ...], ...], ...]
    question_positions: tuple[tuple[int, int], ...]


# --------------------------------------------------------------------------
# 录入校验
# --------------------------------------------------------------------------


def _as_dim(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是整数")
    if isinstance(value, int):
        dim = value
    elif isinstance(value, str) and value.strip().isdigit():
        dim = int(value.strip())
    else:
        raise ValidationError(f"{label}必须是 {MIN_DIM}～{MAX_DIM} 的整数")
    if not (MIN_DIM <= dim <= MAX_DIM):
        raise ValidationError(f"{label}必须在 {MIN_DIM}～{MAX_DIM} 之间")
    return dim


def _as_total(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是非负整数")
    if isinstance(value, int):
        total = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        total = int(value.strip())
    else:
        raise ValidationError(f"{label}必须是非负整数")
    if total < 0:
        raise ValidationError(f"{label}不能为负")
    return total


def _as_interval(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValidationError(f"{label}必须是 [起点, 终点] 形式的码点区间")
    start, end = value
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValidationError(f"{label}的起止必须是整数")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValidationError(f"{label}的起止必须是整数")
    if start < 0 or end <= start:
        raise ValidationError(f"{label}必须满足 0 ≤ 起点 < 终点")
    return start, end


def build_spec(
    rows_in: Any,
    cols_in: Any,
    cells_in: Any,
    row_totals_in: Any,
    col_totals_in: Any,
    body: str,
) -> TableSpec:
    """从表单/JSON 录入构造并完整校验一份统计表规格。"""
    rows = _as_dim(rows_in, "行数")
    cols = _as_dim(cols_in, "列数")
    body_len = len(body)

    if not isinstance(cells_in, list) or len(cells_in) != rows:
        raise ValidationError(f"数据格区间必须是 {rows} 行的二维数组")
    cell_intervals: list[list[tuple[int, int]]] = []
    flat: list[tuple[int, int]] = []
    question_total = 0
    for r, row in enumerate(cells_in):
        if not isinstance(row, list) or len(row) != cols:
            raise ValidationError(f"第 {r + 1} 行必须有 {cols} 个数据格")
        current: list[tuple[int, int]] = []
        for c, raw_cell in enumerate(row):
            label = f"第 {r + 1} 行第 {c + 1} 列数据格"
            start, end = _as_interval(raw_cell, label)
            if end > body_len:
                raise ValidationError(f"{label}区间超出正文（正文共 {body_len} 个码点）")
            text = body[start:end]
            if not text:
                raise ValidationError(f"{label}为空，数据格必须含数字或问号")
            bad = [ch for ch in text if ch not in CELL_CHARS]
            if bad:
                raise ValidationError(
                    f"{label}只允许 0～9 与半角问号，发现非法字符：{''.join(sorted(set(bad)))}"
                )
            if text.count("?") > 1:
                raise ValidationError(f"{label}至多含一个问号")
            question_total += text.count("?")
            current.append((start, end))
            flat.append((start, end))
        cell_intervals.append(current)

    if not (1 <= question_total <= MAX_QUESTIONS):
        raise ValidationError(f"全表问号数必须在 1～{MAX_QUESTIONS} 之间（当前 {question_total} 个）")

    ordered = sorted(flat)
    for (a1, b1), (a2, b2) in zip(ordered, ordered[1:]):
        if a2 < b1:
            raise ValidationError(
                f"数据格区间互不重叠：[{a1}, {b1}) 与 [{a2}, {b2}) 相交"
            )

    if not isinstance(row_totals_in, list) or len(row_totals_in) != rows:
        raise ValidationError(f"行合计必须是 {rows} 个数")
    if not isinstance(col_totals_in, list) or len(col_totals_in) != cols:
        raise ValidationError(f"列合计必须是 {cols} 个数")
    row_totals = tuple(
        _as_total(v, f"第 {r + 1} 行合计") for r, v in enumerate(row_totals_in)
    )
    col_totals = tuple(
        _as_total(v, f"第 {c + 1} 列合计") for c, v in enumerate(col_totals_in)
    )
    if sum(row_totals) != sum(col_totals):
        raise ValidationError(
            f"行合计之和（{sum(row_totals)}）与列合计之和（{sum(col_totals)}）不一致，纸面合计本身矛盾"
        )

    return TableSpec(
        rows=rows,
        cols=cols,
        cells=tuple(tuple(row) for row in cell_intervals),
        row_totals=row_totals,
        col_totals=col_totals,
    )


# --------------------------------------------------------------------------
# 求解
# --------------------------------------------------------------------------


def _grid_from_body(body: str, spec: TableSpec) -> list[list[str]]:
    return [[body[a:b] for a, b in row] for row in spec.cells]


def solve(body: str, spec: TableSpec) -> FillResult:
    """枚举问号的一位数字填法，找出同时满足全部行列合计的解（至多保留两组）。"""
    grid = _grid_from_body(body, spec)
    unknowns: list[tuple[int, int]] = [
        (r, c)
        for r in range(spec.rows)
        for c in range(spec.cols)
        if "?" in grid[r][c]
    ]
    if not unknowns:
        raise ValidationError("表中没有问号，无需补字")

    fixed_row = [0] * spec.rows
    fixed_col = [0] * spec.cols
    for r in range(spec.rows):
        for c in range(spec.cols):
            text = grid[r][c]
            if "?" not in text:
                value = int(text)
                fixed_row[r] += value
                fixed_col[c] += value

    # 每个问号所在格的前缀、后缀决定该格在补入数字 d 时的整格数值。
    parts: list[tuple[int, int, str, str]] = []
    for r, c in unknowns:
        q = grid[r][c].index("?")
        prefix, suffix = grid[r][c][:q], grid[r][c][q + 1 :]
        parts.append((r, c, prefix, suffix))

    row_sum = list(fixed_row)
    col_sum = list(fixed_col)
    digits = [0] * len(unknowns)
    solutions: list[tuple[tuple[str, ...], ...]] = []

    def cell_value(prefix: str, suffix: str, digit: int) -> int:
        return int(f"{prefix}{digit}{suffix}") if prefix or suffix else digit

    def backtrack(i: int) -> None:
        if len(solutions) >= MAX_SOLUTIONS_RETURNED:
            return
        if i == len(unknowns):
            if all(row_sum[r] == spec.row_totals[r] for r in range(spec.rows)) and all(
                col_sum[c] == spec.col_totals[c] for c in range(spec.cols)
            ):
                filled = [row[:] for row in grid]
                for k, (r, c) in enumerate(unknowns):
                    prefix, suffix = parts[k][2], parts[k][3]
                    filled[r][c] = f"{prefix}{digits[k]}{suffix}"
                solutions.append(tuple(tuple(row) for row in filled))
            return

        r, c, prefix, suffix = parts[i]
        for digit in range(10):
            value = cell_value(prefix, suffix, digit)
            row_sum[r] += value
            col_sum[c] += value
            digits[i] = digit
            # 剪枝：只可能继续增大的和不得越过纸面合计。
            if row_sum[r] <= spec.row_totals[r] and col_sum[c] <= spec.col_totals[c]:
                backtrack(i + 1)
            row_sum[r] -= value
            col_sum[c] -= value
            if len(solutions) >= MAX_SOLUTIONS_RETURNED:
                return

    backtrack(0)
    if not solutions:
        status = "none"
    elif len(solutions) == 1:
        status = "unique"
    else:
        status = "multiple"
    return FillResult(
        status=status,
        solutions=tuple(solutions),
        question_positions=tuple(unknowns),
    )


# --------------------------------------------------------------------------
# 唯一解发版
# --------------------------------------------------------------------------


def _rebuild_spans(
    conn: sqlite3.Connection,
    base,
    replacements: dict[int, str],
):
    """按逐字符替换重建新版的同源段列表。

    未改文字沿用原出处：仍是原框线性对应的，保留 linear/map_start；被替换的
    问号变成换入字，继承该问号所在原同源段的整段来源（linear=0），与普通
    校订的继承规则一致。
    """
    spans = load_spans(conn, base.id)
    entries_by_set = load_source_entries(
        conn, {span.source_set_id for span in spans}
    )
    runs: list[tuple[int, int, int, bool, int | None]] = []
    out_pos = 0
    for span in spans:
        set_id = span.source_set_id
        entries = entries_by_set.get(set_id, ())
        if not entries:
            raise RuntimeError("内部错误：来源集合缺少条目")
        inherited_id = get_or_create_source_set(conn, entries)
        seg_start = span.start
        for pos in range(span.start, span.end):
            if pos in replacements:
                if seg_start < pos:
                    map_start = span.map_start
                    if span.linear and map_start is not None:
                        map_start += seg_start - span.start
                    runs.append(
                        (out_pos, out_pos + (pos - seg_start), set_id, span.linear, map_start)
                    )
                    out_pos += pos - seg_start
                # 换入的新数字：继承问号所在段的整段来源，不再线性对应。
                runs.append((out_pos, out_pos + 1, inherited_id, False, None))
                out_pos += 1
                seg_start = pos + 1
        if seg_start < span.end:
            map_start = span.map_start
            if span.linear and map_start is not None:
                map_start += seg_start - span.start
            runs.append(
                (out_pos, out_pos + (span.end - seg_start), set_id, span.linear, map_start)
            )
            out_pos += span.end - seg_start
    return archive._merge_runs(runs)


def fill_table(
    conn: sqlite3.Connection,
    manuscript,
    base_release_id: int,
    spec: TableSpec,
    note: str = "",
):
    """唯一解时在一个写事务内补齐全部问号并随新版保存 CSV 规格。

    所据版本不是现行版时抛 :class:`StaleReleaseError`；无解或多解时抛
    :class:`ValidationError`，由调用方先调 :func:`solve` 区分并向用户展示。
    任何失败都随事务回滚，正文、版本与 CSV 规格均不生效。
    """
    note = note.strip()
    archive.validate_text(note, "补字说明")
    if len(note) > 2000:
        raise ValidationError("补字说明过长")

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

        result = solve(base.body, spec)
        if result.status != "unique":
            if result.status == "none":
                raise ValidationError("行列合计无法同时满足，本表无解；本次不发版")
            raise ValidationError("本表存在多组填法，已列出两组供复核；本次不发版")

        filled = result.solutions[0]
        # 逐字符等长替换：区间互不重叠，按位置直接映射即可。
        # 定位每个问号在正文中的绝对码点，并记下唯一解对应位上的新数字。
        replacements: dict[int, str] = {}
        for r, c in result.question_positions:
            cell_start, cell_end = spec.cells[r][c]
            cell_text = base.body[cell_start:cell_end]
            q = cell_text.index("?")
            replacements[cell_start + q] = filled[r][c][q]

        new_body_chars = list(base.body)
        for pos, digit in replacements.items():
            new_body_chars[pos] = digit
        new_body = "".join(new_body_chars)

        merged = _rebuild_spans(conn, base, replacements)
        coverage = sum(b - a for a, b, *_ in merged)
        if coverage != len(new_body) or not merged:
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
            (
                manuscript.id,
                next_no,
                base.id,
                new_body,
                note or "统计表补字：按行列合计补齐问号",
                created_at,
            ),
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
            "INSERT INTO table_fill "
            "(release_id, manuscript_id, row_count, col_count, cells_json, "
            " row_totals_json, col_totals_json, filled_values_json, question_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                release_id,
                manuscript.id,
                spec.rows,
                spec.cols,
                json.dumps([[list(iv) for iv in row] for row in spec.cells]),
                json.dumps(list(spec.row_totals)),
                json.dumps(list(spec.col_totals)),
                json.dumps([list(row) for row in filled]),
                len(result.question_positions),
            ),
        )
        conn.execute(
            "UPDATE manuscript SET current_release_id = ? WHERE id = ?",
            (release_id, manuscript.id),
        )
        return get_release_by_id(conn, release_id), filled


# --------------------------------------------------------------------------
# 入库后的读取与 CSV
# --------------------------------------------------------------------------


def get_fill_for_release(conn: sqlite3.Connection, release_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM table_fill WHERE release_id = ?", (release_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "release_id": row["release_id"],
        "rows": row["row_count"],
        "cols": row["col_count"],
        "cells": json.loads(row["cells_json"]),
        "row_totals": json.loads(row["row_totals_json"]),
        "col_totals": json.loads(row["col_totals_json"]),
        "filled": json.loads(row["filled_values_json"]),
        "question_count": row["question_count"],
    }


def list_fills(conn: sqlite3.Connection, manuscript_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT release_id FROM table_fill WHERE manuscript_id = ? ORDER BY release_id",
        (manuscript_id,),
    ).fetchall()
    return [get_fill_for_release(conn, r["release_id"]) for r in rows]


def fill_csv(fill: dict) -> str:
    """供数 CSV：数据格严格按原表行列顺序排列，每行一条记录。"""
    lines = [",".join(row) for row in fill["filled"]]
    return "\n".join(lines) + "\n"
