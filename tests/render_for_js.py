"""Render a proofreading page to /tmp for the jsdom selection test.

The page deliberately mixes BMP and supplementary-plane characters and spans
several provenance runs, so the browser-side code-point mapping is exercised.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_archive import archive
from ocr_archive.app import create_app


def main(out_path: Path) -> None:
    db = out_path.with_suffix(".sqlite3")
    if db.exists():
        db.unlink()
    app = create_app({"TESTING": True, "DATABASE": str(db)})
    client = app.test_client()
    boxes = [
        {"page_no": 1, "box_no": 1, "rect": [10, 20, 300, 40], "text": "甲乙𠀀丙"},
        {"page_no": 1, "box_no": 2, "rect": [10, 70, 300, 40], "text": "丁戊己庚"},
        {"page_no": 2, "box_no": 1, "rect": [20, 30, 280, 60], "text": "辛壬癸"},
    ]
    client.post("/api/import", json={"code": "JS-1", "title": "选择测试", "boxes": boxes})
    with app.app_context():
        conn = archive.connect(str(db))
        ms = archive.get_manuscript(conn, "JS-1")
        base = archive.get_release_by_id(conn, ms.current_release_id)
        # Keep 甲乙𠀀丙 intact so the browser test exercises a UTF-16 surrogate
        # pair; the edit lengthens a later run and shifts offsets.
        archive.revise(conn, ms, base.id, 5, 6, "AB", "")
        conn.close()
    resp = client.get("/m/JS-1")
    assert resp.status_code == 200, resp.status_code
    out_path.write_text(resp.get_data(as_text=True), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
