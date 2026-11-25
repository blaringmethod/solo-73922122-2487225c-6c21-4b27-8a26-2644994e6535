from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import HTTPException

from . import archive, tablefill
from .archive import (
    ArchiveError,
    NotFoundError,
    StaleReleaseError,
    ValidationError,
)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "OCR_DATABASE", str(Path(app.instance_path) / "archive.sqlite3")
        ),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        JSON_AS_ASCII=False,
    )
    if test_config:
        app.config.update(test_config)
    app.json.ensure_ascii = False

    app.teardown_appcontext(archive.close_db)

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return _api_error(exc.description or exc.name, exc.code or 500)
        return (
            render_template(
                "error.html",
                code=exc.code or 500,
                name=exc.name or "出错",
                message=exc.description or exc.name,
            ),
            exc.code or 500,
        )

    @app.errorhandler(sqlite3.OperationalError)
    def database_busy(exc: sqlite3.OperationalError):
        # A writer timeout is the one failure a reviewer can retry safely: the
        # transaction was rolled back and no release or provenance row survived.
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise exc
        message = "数据库忙，写入已回滚，未生成新版；请稍后重试。"
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=message, code="database_busy"), 503
        return (
            render_template(
                "error.html", code=503, name="暂不可写", message=message
            ),
            503,
        )

    with app.app_context():
        archive.init_db()

    # ---------- HTML ----------

    @app.get("/")
    def index():
        conn = archive.get_db()
        rows = conn.execute(
            "SELECT m.*, r.release_no AS current_release_no, r.body AS current_body, "
            "(SELECT COUNT(*) FROM release x WHERE x.manuscript_id = m.id) AS release_count "
            "FROM manuscript m LEFT JOIN release r ON r.id = m.current_release_id "
            "ORDER BY m.id"
        ).fetchall()
        return render_template("index.html", manuscripts=rows)

    @app.post("/import")
    def import_page():
        code = request.form.get("code", "")
        title = request.form.get("title", "")
        raw = request.form.get("boxes", "")
        if "file" in request.files and request.files["file"].filename:
            raw = request.files["file"].read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            payload = None
            error = f"识别框 JSON 解析失败：{exc}"
            return render_template(
                "import_error.html", error=error, code=code, title=title, raw=raw
            ), 400
        try:
            boxes = archive.parse_boxes(payload)
            archive.ingest(archive.get_db(), code, title, boxes)
        except ArchiveError as exc:
            return render_template(
                "import_error.html",
                error=str(exc),
                code=code,
                title=title,
                raw=raw,
            ), 400
        return redirect(
            url_for("manuscript_page", code=code.strip())
        )

    @app.get("/m/<code>")
    def manuscript_page(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
        except NotFoundError:
            abort(404)
        if manuscript.current_release_id is None:
            abort(404)
        current = archive.get_release_by_id(conn, manuscript.current_release_id)
        requested = request.args.get("release", type=int)
        release = current
        if requested and requested != current.release_no:
            try:
                release = archive.get_release(conn, manuscript.id, requested)
            except NotFoundError:
                abort(404)
        highlight = _parse_range_args(request.args)
        lookup_start = request.args.get("lookup_start", type=int)
        lookup_end = request.args.get("lookup_end", type=int)
        page_filter = request.args.get("page", type=int)

        lookup = None
        lookup_error = None
        lookup_target = None
        if lookup_start is not None or lookup_end is not None:
            try:
                if lookup_start is None or lookup_end is None:
                    raise ValidationError("查源需要同时给出起点和终点")
                lookup_target = release
                lookup = archive.lookup_sources(
                    conn, lookup_target, lookup_start, lookup_end, page_filter
                )
                if highlight is None:
                    highlight = (lookup_start, lookup_end)
            except ArchiveError as exc:
                lookup_error = str(exc)

        return render_template(
            "manuscript.html",
            manuscript=manuscript,
            release=release,
            current=current,
            releases=archive.list_releases(conn, manuscript.id),
            markup=archive.body_markup(conn, release, highlight),
            boxes=_box_rows(conn, manuscript.id),
            highlight=highlight,
            lookup=lookup,
            lookup_error=lookup_error,
            lookup_target=lookup_target,
            lookup_start=lookup_start,
            lookup_end=lookup_end,
            page_filter=page_filter,
            fill=tablefill.get_fill_for_release(conn, release.id),
            fill_by_release={
                f["release_id"]: f for f in tablefill.list_fills(conn, manuscript.id)
            },
        )

    @app.get("/m/<code>/r/<int:release_no>")
    def release_page(code: str, release_no: int):
        query = {k: v for k, v in request.args.items()}
        query["release"] = str(release_no)
        return redirect(url_for("manuscript_page", code=code, **query), code=303)

    @app.post("/m/<code>/revise")
    def revise_page(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
        except NotFoundError:
            abort(404)
        form = request.form
        try:
            base_id = _required_int(form, "base_release_id", "依据版本")
            start = _required_int(form, "start", "起点")
            end = _required_int(form, "end", "终点")
            replacement = form.get("replacement", "")
            note = form.get("note", "")
            if start >= end:
                raise ValidationError("请选择一段非空正文（起点必须小于终点）")
            updated = archive.revise(
                conn, manuscript, base_id, start, end, replacement, note
            )
        except StaleReleaseError as exc:
            manuscript, release = _fresh_current(conn, code)
            return _render_manuscript_error(
                conn,
                manuscript,
                release,
                f"校订失败：{exc}。现行版为第 {exc.current_release_no} 版，请刷新后基于现行版重做。",
                409,
            )
        except NotFoundError:
            abort(404)
        except ArchiveError as exc:
            manuscript, release = _fresh_current(conn, code)
            return _render_manuscript_error(
                conn, manuscript, release, f"校订失败：{exc}", 400
            )
        return redirect(
            url_for(
                "manuscript_page",
                code=code,
                release=updated.release_no,
                highlight=f"{start},{start + len(replacement)}"
                if replacement
                else None,
            )
        )

    @app.get("/m/<code>/lookup")
    def lookup_page(code: str):
        args = {k: v for k, v in request.args.items()}
        return redirect(url_for("manuscript_page", code=code, **args), code=303)

    # ---------- 统计表补字（HTML 表单） ----------

    @app.post("/m/<code>/table-fill")
    def table_fill_page(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
        except NotFoundError:
            abort(404)
        form = request.form
        current = archive.get_release_by_id(conn, manuscript.current_release_id)
        try:
            base_id = _required_int(form, "base_release_id", "依据版本")
            rows = _required_int(form, "rows", "行数")
            cols = _required_int(form, "cols", "列数")
            spec = tablefill.build_spec(
                rows,
                cols,
                _parse_cell_intervals(form, "cells"),
                _parse_total_list(form, "row_total", rows, "行合计"),
                _parse_total_list(form, "col_total", cols, "列合计"),
                current.body,
            )
            result = tablefill.solve(current.body, spec)
        except ArchiveError as exc:
            return _render_manuscript_error(
                conn, manuscript, current, f"补字失败：{exc}", 400
            )

        if result.status != "unique":
            # 无解或多解：按要求不发版，把结论与（至多两组）填法交回页面复核。
            return _render_fill_outcome(
                conn,
                manuscript,
                current,
                spec,
                result,
                status=200,
            )

        try:
            updated, filled = tablefill.fill_table(
                conn, manuscript, base_id, spec, form.get("note", "")
            )
        except StaleReleaseError as exc:
            fresh = archive.get_release_by_id(
                conn, archive.get_manuscript(conn, code).current_release_id
            )
            return _render_manuscript_error(
                conn,
                manuscript,
                fresh,
                f"补字失败：{exc}。现行版为第 {exc.current_release_no} 版，请刷新后基于现行版重做。",
                409,
            )
        except ArchiveError as exc:
            return _render_manuscript_error(
                conn, manuscript, current, f"补字失败：{exc}", 400
            )
        return redirect(
            url_for(
                "manuscript_page",
                code=code,
                release=updated.release_no,
            ),
            code=303,
        )

    @app.get("/m/<code>/r/<int:release_no>/table.csv")
    def table_fill_csv_page(code: str, release_no: int):
        return _csv_response(code, release_no)

    @app.get("/m/<code>/table.csv")
    def table_fill_csv_current(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
        except NotFoundError:
            abort(404)
        release = archive.get_release_by_id(conn, manuscript.current_release_id)
        return _csv_response(code, release.release_no)

    def _csv_response(code: str, release_no: int):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
            release = archive.get_release(conn, manuscript.id, release_no)
        except NotFoundError:
            abort(404)
        fill = tablefill.get_fill_for_release(conn, release.id)
        if fill is None:
            abort(404)
        csv_text = tablefill.fill_csv(fill)
        filename = f"{code}-r{release_no}.csv"
        return Response(
            csv_text,
            content_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/healthz")
    def healthz():
        archive.get_db().execute("SELECT 1").fetchone()
        return jsonify(ok=True)

    # ---------- JSON API ----------

    @app.post("/api/import")
    def api_import():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("请求体必须是 JSON 对象", 400)
        try:
            boxes = archive.parse_boxes(payload.get("boxes"))
            manuscript_id = archive.ingest(
                archive.get_db(),
                str(payload.get("code", "")),
                str(payload.get("title", "")),
                boxes,
            )
        except ArchiveError as exc:
            return _api_error(str(exc), 400)
        conn = archive.get_db()
        manuscript = archive.get_release_by_id(
            conn,
            conn.execute(
                "SELECT current_release_id FROM manuscript WHERE id = ?",
                (manuscript_id,),
            ).fetchone()["current_release_id"],
        )
        return (
            jsonify(
                ok=True,
                manuscript_id=manuscript_id,
                release_no=manuscript.release_no,
                release_id=manuscript.id,
                body=manuscript.body,
                url=url_for("manuscript_page", code=str(payload.get("code", "")).strip()),
            ),
            201,
        )

    @app.post("/api/m/<code>/revise")
    def api_revise(code: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("请求体必须是 JSON 对象", 400)
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
            updated = archive.revise(
                conn,
                manuscript,
                _payload_int(payload, "base_release_id", "依据版本"),
                _payload_int(payload, "start", "起点"),
                _payload_int(payload, "end", "终点"),
                payload.get("replacement", ""),
                str(payload.get("note", "")),
            )
        except StaleReleaseError as exc:
            return jsonify(
                ok=False,
                error=str(exc),
                code="stale_release",
                current_release_no=exc.current_release_no,
            ), 409
        except NotFoundError as exc:
            return _api_error(str(exc), 404)
        except ArchiveError as exc:
            return _api_error(str(exc), 400)
        return jsonify(
            ok=True,
            release_no=updated.release_no,
            release_id=updated.id,
            body=updated.body,
            url=url_for(
                "manuscript_page", code=code, release=updated.release_no
            ),
        )

    @app.post("/api/m/<code>/table-fill")
    def api_table_fill(code: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("请求体必须是 JSON 对象", 400)
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
            current = archive.get_release_by_id(conn, manuscript.current_release_id)
            base_id = _payload_int(payload, "base_release_id", "依据版本")
            spec = tablefill.build_spec(
                payload.get("rows"),
                payload.get("cols"),
                payload.get("cells"),
                payload.get("row_totals"),
                payload.get("col_totals"),
                current.body,
            )
            result = tablefill.solve(current.body, spec)
        except NotFoundError as exc:
            return _api_error(str(exc), 404)
        except ArchiveError as exc:
            return _api_error(str(exc), 400)

        if result.status != "unique":
            return jsonify(
                ok=True,
                status=result.status,
                published=False,
                question_positions=[
                    {"row": r, "col": c} for r, c in result.question_positions
                ],
                solutions=[
                    {"cells": [list(row) for row in solution]}
                    for solution in result.solutions
                ],
            )

        try:
            updated, filled = tablefill.fill_table(
                conn, manuscript, base_id, spec, str(payload.get("note", ""))
            )
        except StaleReleaseError as exc:
            return jsonify(
                ok=False,
                error=str(exc),
                code="stale_release",
                current_release_no=exc.current_release_no,
            ), 409
        except NotFoundError as exc:
            return _api_error(str(exc), 404)
        except ArchiveError as exc:
            return _api_error(str(exc), 400)
        fill = tablefill.get_fill_for_release(conn, updated.id)
        return jsonify(
            ok=True,
            status="unique",
            published=True,
            release_no=updated.release_no,
            release_id=updated.id,
            body=updated.body,
            filled=[list(row) for row in filled],
            csv_url=url_for(
                "table_fill_csv_page",
                code=code,
                release_no=updated.release_no,
            ),
            table_fill=_fill_json(fill),
            url=url_for(
                "manuscript_page", code=code, release=updated.release_no
            ),
        )

    @app.get("/api/m/<code>/releases")
    def api_releases(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
        except NotFoundError as exc:
            return _api_error(str(exc), 404)
        releases = archive.list_releases(conn, manuscript.id)
        fills = {f["release_id"]: f for f in tablefill.list_fills(conn, manuscript.id)}
        return jsonify(
            ok=True,
            current_release_no=next(
                r.release_no
                for r in releases
                if r.id == manuscript.current_release_id
            ),
            releases=[
                {
                    "release_no": r.release_no,
                    "release_id": r.id,
                    "parent_release_no": _release_no(conn, r.parent_id),
                    "note": r.note,
                    "body": r.body,
                    "created_at": r.created_at,
                    "table_fill": (
                        {
                            **_fill_json(fills[r.id]),
                            "csv_url": url_for(
                                "table_fill_csv_page",
                                code=code,
                                release_no=r.release_no,
                            ),
                        }
                        if r.id in fills
                        else None
                    ),
                }
                for r in releases
            ],
            current_release_id=manuscript.current_release_id,
        )

    @app.get("/api/m/<code>/lookup")
    def api_lookup(code: str):
        conn = archive.get_db()
        try:
            manuscript = archive.get_manuscript(conn, code)
            release_no = request.args.get("release", type=int)
            release = (
                archive.get_release(conn, manuscript.id, release_no)
                if release_no
                else archive.get_release_by_id(conn, manuscript.current_release_id)
            )
            start = request.args.get("start", type=int)
            end = request.args.get("end", type=int)
            if start is None or end is None:
                raise ValidationError("查源需要 start 和 end")
            page_no = request.args.get("page", type=int)
            sources = archive.lookup_sources(conn, release, start, end, page_no)
        except NotFoundError as exc:
            return _api_error(str(exc), 404)
        except ArchiveError as exc:
            return _api_error(str(exc), 400)
        return jsonify(
            ok=True,
            release_no=release.release_no,
            start=start,
            end=end,
            text=release.body[start:end],
            sources=[
                {
                    "page_no": s["page_no"],
                    "box_no": s["box_no"],
                    "rect": s["rect"],
                    "box_intervals": [
                        {"start": a, "end": b} for a, b in s["intervals"]
                    ],
                    "exact": s["exact"],
                    "body_ranges": [
                        {"start": a, "end": b} for a, b in s["body_ranges"]
                    ],
                    "chars": s["chars"],
                }
                for s in sources
            ],
        )

    return app


def _required_int(form, field: str, label: str) -> int:
    value = form.get(field)
    if value is None or not str(value).strip():
        raise ValidationError(f"缺少{label}")
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{label}必须是整数") from exc


def _payload_int(payload: dict, field: str, label: str) -> int:
    value = payload.get(field)
    if value is None or isinstance(value, bool):
        raise ValidationError(f"缺少{label}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是整数") from exc


def _parse_range_args(args) -> tuple[int, int] | None:
    start = args.get("highlight", "")
    if not start:
        return None
    parts = start.split(",")
    if len(parts) != 2:
        return None
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (a, b) if a < b else None


def _box_rows(conn, manuscript_id: int) -> list:
    return conn.execute(
        "SELECT page_no, box_no, x, y, w, h, text FROM box "
        "WHERE manuscript_id = ? ORDER BY reading_pos",
        (manuscript_id,),
    ).fetchall()


def _release_no(conn, release_id: int | None) -> int | None:
    if release_id is None:
        return None
    row = conn.execute(
        "SELECT release_no FROM release WHERE id = ?", (release_id,)
    ).fetchone()
    return row["release_no"] if row else None


def _api_error(message: str, status: int):
    return jsonify(ok=False, error=message), status


def _parse_cell_intervals(form, field: str) -> list:
    """The HTML form posts the 2-D interval array as one JSON textarea."""
    raw = form.get(field, "")
    if not raw.strip():
        raise ValidationError("缺少数据格码点区间")
    try:
        cells = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"数据格区间 JSON 解析失败：{exc}") from exc
    return cells


def _parse_total_list(form, field: str, expected: int, label: str) -> list[int]:
    raw = form.get(field, "")
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if len(parts) != expected:
        raise ValidationError(f"{label}必须是 {expected} 个逗号分隔的整数")
    totals: list[int] = []
    for part in parts:
        try:
            totals.append(int(part))
        except ValueError as exc:
            raise ValidationError(f"{label}必须是整数") from exc
    return totals


def _fill_json(fill: dict | None) -> dict | None:
    if fill is None:
        return None
    return {
        "rows": fill["rows"],
        "cols": fill["cols"],
        "cells": fill["cells"],
        "row_totals": fill["row_totals"],
        "col_totals": fill["col_totals"],
        "filled": fill["filled"],
        "question_count": fill["question_count"],
    }


def _render_fill_outcome(conn, manuscript, release, spec, result, status: int):
    """No-solution / multi-solution answers stay on the page and never publish."""
    return (
        render_template(
            "manuscript.html",
            manuscript=manuscript,
            release=release,
            current=release,
            releases=archive.list_releases(conn, manuscript.id),
            markup=archive.body_markup(conn, release),
            boxes=_box_rows(conn, manuscript.id),
            highlight=None,
            lookup=None,
            lookup_error=None,
            lookup_target=None,
            lookup_start=None,
            lookup_end=None,
            page_filter=None,
            fill=tablefill.get_fill_for_release(conn, release.id),
            fill_by_release={
                f["release_id"]: f for f in tablefill.list_fills(conn, manuscript.id)
            },
            fill_outcome={
                "status": result.status,
                "spec": spec,
                "question_positions": result.question_positions,
                "solutions": result.solutions,
            },
        ),
        status,
    )


def _fresh_current(conn, code: str):
    manuscript = archive.get_manuscript(conn, code)
    release = archive.get_release_by_id(conn, manuscript.current_release_id)
    return manuscript, release


def _render_manuscript_error(conn, manuscript, release, message: str, status: int):
    return (
        render_template(
            "manuscript.html",
            manuscript=manuscript,
            release=release,
            current=release,
            releases=archive.list_releases(conn, manuscript.id),
            markup=archive.body_markup(conn, release),
            boxes=_box_rows(conn, manuscript.id),
            highlight=None,
            lookup=None,
            lookup_error=message,
            lookup_target=None,
            lookup_start=None,
            lookup_end=None,
            page_filter=None,
            fill=tablefill.get_fill_for_release(conn, release.id),
            fill_by_release={
                f["release_id"]: f for f in tablefill.list_fills(conn, manuscript.id)
            },
        ),
        status,
    )
