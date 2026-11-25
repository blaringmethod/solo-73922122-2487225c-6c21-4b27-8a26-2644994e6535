from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ocr_archive import archive, tablefill
from ocr_archive.app import create_app


# 正文布局（与 sample/table_boxes.json 的拼接结果一致，共 19 个码点）：
#   标题框 9 字 → "1? 2? "（含空格）→ "4 5 "
# 数据格区间跳过空格，互不重叠。
BODY = "本埠各業商號統計表1? 2? 4 5 "
CELLS = [[[9, 11], [12, 14]], [[15, 16], [17, 18]]]
# 唯一解：14 / 26 / 4 / 5，行合计 40、9，列合计 18、31。
ROW_TOTALS = [40, 9]
COL_TOTALS = [18, 31]

BOXES = [
    {"page_no": 1, "box_no": 1, "rect": [72, 58, 420, 52], "text": "本埠各業商號統計表"},
    {"page_no": 1, "box_no": 2, "rect": [72, 138, 420, 40], "text": "1? 2? "},
    {"page_no": 1, "box_no": 3, "rect": [72, 196, 420, 40], "text": "4 5 "},
]


class TableFillBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path})
        self.client = self.app.test_client()
        with self.app.app_context():
            self.conn = archive.connect(self.db_path)
            archive.init_db(self.db_path)
            self.mid = archive.ingest(
                self.conn, "T-1", "统计表", archive.parse_boxes(BOXES)
            )
            self.ms = archive.get_manuscript(self.conn, "T-1")
            self.r1 = archive.get_release(self.conn, self.mid, 1)
            self.assertEqual(self.r1.body, BODY)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def spec(self, rows=2, cols=2, cells=None, rt=None, ct=None, body=None):
        return tablefill.build_spec(
            rows,
            cols,
            CELLS if cells is None else cells,
            ROW_TOTALS if rt is None else rt,
            COL_TOTALS if ct is None else ct,
            BODY if body is None else body,
        )


class SpecValidationTest(TableFillBase):
    def test_dimensions_must_be_2_to_4(self):
        for rows in (1, 5):
            with self.assertRaises(tablefill.ValidationError):
                self.spec(rows=rows)
        for cols in (0, 4.5):
            with self.assertRaises(tablefill.ValidationError):
                self.spec(cols=cols)

    def test_shape_must_match_dimensions(self):
        with self.assertRaises(tablefill.ValidationError):
            self.spec(cells=[[[9, 11]]])  # 声明 2 行只给 1 行
        with self.assertRaises(tablefill.ValidationError):
            self.spec(cells=[[[9, 11]], [[15, 16]]])  # 每行只有 1 格

    def test_interval_must_be_half_open_inside_body(self):
        with self.assertRaises(tablefill.ValidationError):
            self.spec(cells=[[[9, 9], [12, 14]], [[15, 16], [17, 18]]])
        with self.assertRaises(tablefill.ValidationError):
            self.spec(cells=[[[9, 99], [12, 14]], [[15, 16], [17, 18]]])
        with self.assertRaises(tablefill.ValidationError):
            self.spec(cells=[[["9", 11], [12, 14]], [[15, 16], [17, 18]]])

    def test_intervals_must_not_overlap(self):
        body = "?234"
        cells = [[[0, 2], [1, 3]], [[2, 3], [3, 4]]]
        with self.assertRaises(tablefill.ValidationError) as ctx:
            self.spec(body=body, cells=cells, rt=[0, 0], ct=[0, 0])
        self.assertIn("互不重叠", str(ctx.exception))

    def test_cell_charset_and_question_limit(self):
        # 非法字符（字母 a）
        body = "1?a?45"
        cells = [[[0, 2], [2, 4]], [[4, 5], [5, 6]]]
        with self.assertRaises(tablefill.ValidationError) as ctx:
            self.spec(body=body, cells=cells, rt=[0, 0], ct=[0, 0])
        self.assertIn("只允许", str(ctx.exception))
        # 同一格两个问号
        body = "??1234"
        cells = [[[0, 2], [2, 3]], [[3, 4], [4, 6]]]
        with self.assertRaises(tablefill.ValidationError) as ctx:
            self.spec(body=body, cells=cells, rt=[0, 0], ct=[0, 0])
        self.assertIn("至多含一个问号", str(ctx.exception))

    def test_question_count_between_1_and_6(self):
        body = "1234"
        cells = [[[0, 1], [1, 2]], [[2, 3], [3, 4]]]
        with self.assertRaises(tablefill.ValidationError) as ctx:
            self.spec(body=body, cells=cells, rt=[0, 0], ct=[0, 0])
        self.assertIn("问号数", str(ctx.exception))

    def test_totals_shape_and_sign(self):
        with self.assertRaises(tablefill.ValidationError):
            self.spec(rt=[40])
        with self.assertRaises(tablefill.ValidationError):
            self.spec(ct=[18, 31, 9])
        with self.assertRaises(tablefill.ValidationError):
            self.spec(rt=[40, -1])

    def test_grand_totals_must_agree(self):
        with self.assertRaises(tablefill.ValidationError) as ctx:
            self.spec(rt=[40, 9], ct=[18, 30])
        self.assertIn("不一致", str(ctx.exception))


class SolveTest(TableFillBase):
    def test_unique_solution_for_sample_table(self):
        result = tablefill.solve(BODY, self.spec())
        self.assertEqual(result.status, "unique")
        self.assertEqual(result.solutions, ((("14", "26"), ("4", "5")),))
        self.assertEqual(result.question_positions, ((0, 0), (0, 1)))

    def test_multi_digit_cell_with_question(self):
        # 单元格 "12?" 与 "4"、"5"、"6"；?=3 时 123+4=127、5+6=11
        body = "12?456"
        cells = [[[0, 3], [3, 4]], [[4, 5], [5, 6]]]
        spec = self.spec(cells=cells, body=body, rt=[127, 11], ct=[128, 10])
        result = tablefill.solve(body, spec)
        self.assertEqual(result.status, "unique")
        self.assertEqual(result.solutions[0][0][0], "123")

    def test_no_solution_reports_none_and_no_candidates(self):
        # 总和一致（108），但任何单位数填法都达不到行合计 99。
        spec = self.spec(rt=[99, 9], ct=[100, 8])
        result = tablefill.solve(BODY, spec)
        self.assertEqual(result.status, "none")
        self.assertEqual(result.solutions, ())

    def test_multiple_solutions_returns_two_for_review(self):
        body = "????"
        cells = [[[0, 1], [1, 2]], [[2, 3], [3, 4]]]
        spec = self.spec(cells=cells, body=body, rt=[1, 1], ct=[1, 1])
        result = tablefill.solve(body, spec)
        self.assertEqual(result.status, "multiple")
        self.assertEqual(len(result.solutions), 2)
        for solution in result.solutions:
            self.assertEqual(
                [sum(int(v) for v in row) for row in solution], [1, 1]
            )
            self.assertEqual(
                [sum(int(solution[r][c]) for r in range(2)) for c in range(2)],
                [1, 1],
            )

    def test_4x4_table_with_six_questions(self):
        # 4x4、每格一位，共 6 个问号；按 ?=9 生成纸面合计。
        body = "?2?4?6?8?012345?"
        self.assertEqual(len(body), 16)
        self.assertEqual(body.count("?"), 6)
        cells = [[[r * 4 + c, r * 4 + c + 1] for c in range(4)] for r in range(4)]
        filled = [[body[r * 4 + c].replace("?", "9") for c in range(4)] for r in range(4)]
        rt = [sum(int(v) for v in row) for row in filled]
        ct = [sum(int(filled[r][c]) for r in range(4)) for c in range(4)]
        spec = self.spec(rows=4, cols=4, cells=cells, body=body, rt=rt, ct=ct)
        result = tablefill.solve(body, spec)
        # 返回的任何候选都必须同时满足全部行列合计。
        for solution in result.solutions:
            self.assertEqual([sum(int(v) for v in row) for row in solution], rt)
            self.assertEqual(
                [sum(int(solution[r][c]) for r in range(4)) for c in range(4)], ct
            )
        if result.status == "unique":
            self.assertEqual(result.solutions[0], tuple(tuple(row) for row in filled))
        else:
            self.assertEqual(result.status, "multiple")
            self.assertEqual(len(result.solutions), 2)


class FillPublishingTest(TableFillBase):
    def test_unique_fill_publishes_new_release_and_csv_spec(self):
        before = self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"]
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            release, filled = tablefill.fill_table(
                self.conn, ms, self.r1.id, self.spec(), "纸面合计补字"
            )
        self.assertEqual(release.release_no, 2)
        self.assertEqual(release.parent_id, self.r1.id)
        self.assertEqual(release.note, "纸面合计补字")
        self.assertEqual(release.body, "本埠各業商號統計表14 26 4 5 ")
        self.assertEqual(len(release.body), len(self.r1.body))  # 等长替换
        self.assertEqual(filled, (("14", "26"), ("4", "5")))
        self.assertEqual(
            archive.get_manuscript(self.conn, "T-1").current_release_id, release.id
        )
        # 旧版快照不变。
        self.assertEqual(archive.get_release(self.conn, self.mid, 1).body, BODY)

        fill = tablefill.get_fill_for_release(self.conn, release.id)
        self.assertIsNotNone(fill)
        self.assertEqual(fill["rows"], 2)
        self.assertEqual(fill["cols"], 2)
        self.assertEqual(fill["cells"], CELLS)
        self.assertEqual(fill["row_totals"], ROW_TOTALS)
        self.assertEqual(fill["col_totals"], COL_TOTALS)
        self.assertEqual(fill["filled"], [["14", "26"], ["4", "5"]])
        self.assertEqual(fill["question_count"], 2)
        # 供数 CSV：数据格按原表行列顺序，每行一条记录。
        self.assertEqual(tablefill.fill_csv(fill), "14,26\n4,5\n")
        # 旧版没有补字记录。
        self.assertIsNone(tablefill.get_fill_for_release(self.conn, self.r1.id))
        after = self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"]
        self.assertEqual(after, before + 1)

    def test_filled_digit_inherits_question_source_untouched_keeps_exact(self):
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            release, _ = tablefill.fill_table(self.conn, ms, self.r1.id, self.spec())
        # 新数字「4」（原问号，正文偏移 10）：换入字，继承框 2 的整段区间，exact=False。
        sources = archive.lookup_sources(self.conn, release, 10, 11)
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]["page_no"], sources[0]["box_no"]), (1, 2))
        self.assertEqual(sources[0]["intervals"], [(0, 6)])
        self.assertFalse(sources[0]["exact"])
        # 未改的「1」（偏移 9）仍精确对应框内 [0,1)。
        sources = archive.lookup_sources(self.conn, release, 9, 10)
        self.assertTrue(sources[0]["exact"])
        self.assertEqual(sources[0]["intervals"], [(0, 1)])
        # 第二行未改数字仍指向框 3 的精确位置。
        sources = archive.lookup_sources(self.conn, release, 15, 16)
        self.assertEqual((sources[0]["page_no"], sources[0]["box_no"]), (1, 3))
        self.assertEqual(sources[0]["intervals"], [(0, 1)])
        self.assertTrue(sources[0]["exact"])

    def test_fill_after_prior_revision_inherits_replacement_source(self):
        # 先普通校订把问号「?」改成另一个问号（换入字，linear=0），再补字。
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            r2 = archive.revise(self.conn, ms, self.r1.id, 10, 11, "?", "复核问号")
            release, _ = tablefill.fill_table(
                self.conn,
                archive.get_manuscript(self.conn, "T-1"),
                r2.id,
                self.spec(),
            )
        sources = archive.lookup_sources(self.conn, release, 10, 11)
        self.assertFalse(sources[0]["exact"])
        self.assertEqual((sources[0]["page_no"], sources[0]["box_no"]), (1, 2))

    def test_no_solution_and_multiple_solution_publish_nothing(self):
        for rt, ct in [([99, 9], [100, 8])]:  # 无解
            with self.app.app_context():
                ms = archive.get_manuscript(self.conn, "T-1")
                with self.assertRaises(tablefill.ValidationError):
                    tablefill.fill_table(
                        self.conn, ms, self.r1.id, self.spec(rt=rt, ct=ct)
                    )
        body = "????"
        cells = [[[0, 1], [1, 2]], [[2, 3], [3, 4]]]
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            with self.assertRaises(tablefill.ValidationError):
                tablefill.fill_table(
                    self.conn,
                    ms,
                    self.r1.id,
                    self.spec(body=body, cells=cells, rt=[1, 1], ct=[1, 1]),
                )
        # 没有任何新版、补字记录或正文变化。
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM table_fill").fetchone()["n"], 0
        )
        self.assertEqual(
            archive.get_manuscript(self.conn, "T-1").current_release_id, self.r1.id
        )

    def test_stale_base_is_rejected_and_nothing_is_written(self):
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            r2 = archive.revise(self.conn, ms, self.r1.id, 0, 1, "X", "先改一笔")
            with self.assertRaises(archive.StaleReleaseError) as ctx:
                tablefill.fill_table(
                    self.conn,
                    archive.get_manuscript(self.conn, "T-1"),
                    self.r1.id,
                    self.spec(),
                )
            self.assertEqual(ctx.exception.current_release_no, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM table_fill").fetchone()["n"], 0
        )
        self.assertEqual(
            archive.get_manuscript(self.conn, "T-1").current_release_id, r2.id
        )

    def test_write_failure_rolls_back_release_spans_and_csv(self):
        release_before = self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"]
        span_before = self.conn.execute("SELECT COUNT(*) AS n FROM body_span").fetchone()["n"]
        fill_before = self.conn.execute("SELECT COUNT(*) AS n FROM table_fill").fetchone()["n"]
        # 触发器在 release/body_span 已写入、即将写 table_fill 时中止，
        # 用来证明补字与 CSV 规格在同一事务里，一笔失败全部回滚。
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_fill BEFORE INSERT ON table_fill "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            with self.assertRaises(sqlite3.DatabaseError):
                tablefill.fill_table(self.conn, ms, self.r1.id, self.spec())
        self.conn.execute("DROP TRIGGER fail_fill")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"],
            release_before,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM body_span").fetchone()["n"],
            span_before,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM table_fill").fetchone()["n"],
            fill_before,
        )
        # 现行版与正文都停留在第 1 版。
        ms = archive.get_manuscript(self.conn, "T-1")
        self.assertEqual(ms.current_release_id, self.r1.id)
        self.assertEqual(
            archive.get_release_by_id(self.conn, ms.current_release_id).body, BODY
        )

    def test_proofing_remains_available_between_and_after_fills(self):
        with self.app.app_context():
            ms = archive.get_manuscript(self.conn, "T-1")
            r2, _ = tablefill.fill_table(self.conn, ms, self.r1.id, self.spec())
            r3 = archive.revise(
                self.conn,
                archive.get_manuscript(self.conn, "T-1"),
                r2.id,
                0,
                1,
                "全",
                "核算期间继续校订标题",
            )
        self.assertEqual(r3.body[0], "全")
        # 补字产生的数字仍在。
        self.assertIn("14", r3.body)
        fills = tablefill.list_fills(self.conn, self.mid)
        self.assertEqual([f["release_id"] for f in fills], [r2.id])


class HttpTableFillTest(TableFillBase):
    def form_payload(self, **overrides):
        data = {
            "base_release_id": self.r1.id,
            "rows": "2",
            "cols": "2",
            "cells": json.dumps(CELLS),
            "row_total": ",".join(str(v) for v in ROW_TOTALS),
            "col_total": ",".join(str(v) for v in COL_TOTALS),
            "note": "网页补字",
        }
        data.update(overrides)
        return data

    def test_html_unique_fill_redirects_and_csv_downloads(self):
        resp = self.client.post(
            "/m/T-1/table-fill", data=self.form_payload()
        )
        self.assertIn(resp.status_code, (302, 303))
        page = self.client.get("/m/T-1").get_data(as_text=True)
        self.assertIn("14", page)
        self.assertIn("统计表补字", page)
        csv_resp = self.client.get("/m/T-1/r/2/table.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertEqual(csv_resp.mimetype, "text/csv")
        self.assertIn("charset=utf-8", csv_resp.content_type)
        self.assertIn("attachment", csv_resp.headers["Content-Disposition"])
        self.assertEqual(csv_resp.get_data(as_text=True), "14,26\n4,5\n")
        # 现行版快捷下载同样可用。
        current_csv = self.client.get("/m/T-1/table.csv")
        self.assertEqual(current_csv.status_code, 200)
        self.assertEqual(current_csv.get_data(), csv_resp.get_data())

    def test_html_no_solution_stays_on_page_without_publishing(self):
        resp = self.client.post(
            "/m/T-1/table-fill",
            data=self.form_payload(row_total="99,9", col_total="100,8"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("无解", resp.get_data(as_text=True))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"], 1
        )

    def test_html_multiple_solutions_lists_two_candidates(self):
        self.client.post(
            "/api/import",
            json={
                "code": "T-Q",
                "title": "全问号",
                "boxes": [
                    {"page_no": 1, "box_no": 1, "rect": [0, 0, 10, 10], "text": "????"}
                ],
            },
        )
        r1_id = self.conn.execute(
            "SELECT id FROM release WHERE manuscript_id = "
            "(SELECT id FROM manuscript WHERE code='T-Q')"
        ).fetchone()["id"]
        resp = self.client.post(
            "/m/T-Q/table-fill",
            data={
                "base_release_id": r1_id,
                "rows": "2",
                "cols": "2",
                "cells": json.dumps([[[0, 1], [1, 2]], [[2, 3], [3, 4]]]),
                "row_total": "1,1",
                "col_total": "1,1",
            },
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("多解", html)
        self.assertEqual(html.count("候选填法"), 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM release WHERE manuscript_id = "
                "(SELECT id FROM manuscript WHERE code='T-Q')"
            ).fetchone()["n"],
            1,
        )

    def test_html_validation_error_renders_manuscript_page(self):
        resp = self.client.post(
            "/m/T-1/table-fill", data=self.form_payload(rows="1")
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("补字失败", resp.get_data(as_text=True))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM release").fetchone()["n"], 1
        )

    def test_api_unique_fill_payload_and_csv_url(self):
        resp = self.client.post(
            "/api/m/T-1/table-fill",
            json={
                "base_release_id": self.r1.id,
                "rows": 2,
                "cols": 2,
                "cells": CELLS,
                "row_totals": ROW_TOTALS,
                "col_totals": COL_TOTALS,
                "note": "接口补字",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["status"], "unique")
        self.assertEqual(data["filled"], [["14", "26"], ["4", "5"]])
        self.assertEqual(data["release_no"], 2)
        csv_resp = self.client.get(data["csv_url"])
        self.assertEqual(csv_resp.get_data(as_text=True), "14,26\n4,5\n")
        self.assertEqual(data["table_fill"]["question_count"], 2)

    def test_api_none_and_multiple_do_not_publish(self):
        resp = self.client.post(
            "/api/m/T-1/table-fill",
            json={
                "base_release_id": self.r1.id,
                "rows": 2,
                "cols": 2,
                "cells": CELLS,
                "row_totals": [99, 9],
                "col_totals": [100, 8],
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["published"])
        self.assertEqual(data["status"], "none")
        self.assertEqual(data["solutions"], [])

        self.client.post(
            "/api/import",
            json={
                "code": "T-Q2",
                "title": "全问号",
                "boxes": [
                    {"page_no": 1, "box_no": 1, "rect": [0, 0, 10, 10], "text": "????"}
                ],
            },
        )
        q_id = self.conn.execute(
            "SELECT id FROM release WHERE manuscript_id = "
            "(SELECT id FROM manuscript WHERE code='T-Q2')"
        ).fetchone()["id"]
        resp = self.client.post(
            "/api/m/T-Q2/table-fill",
            json={
                "base_release_id": q_id,
                "rows": 2,
                "cols": 2,
                "cells": [[[0, 1], [1, 2]], [[2, 3], [3, 4]]],
                "row_totals": [1, 1],
                "col_totals": [1, 1],
            },
        )
        data = resp.get_json()
        self.assertFalse(data["published"])
        self.assertEqual(data["status"], "multiple")
        self.assertEqual(len(data["solutions"]), 2)

    def test_api_stale_base_returns_409_and_writes_nothing(self):
        self.client.post(
            "/api/m/T-1/revise",
            json={"base_release_id": self.r1.id, "start": 0, "end": 1, "replacement": "X"},
        )
        resp = self.client.post(
            "/api/m/T-1/table-fill",
            json={
                "base_release_id": self.r1.id,
                "rows": 2,
                "cols": 2,
                "cells": CELLS,
                "row_totals": ROW_TOTALS,
                "col_totals": COL_TOTALS,
            },
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "stale_release")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM table_fill").fetchone()["n"], 0
        )

    def test_releases_api_carries_table_fill_and_csv_url(self):
        self.client.post(
            "/api/m/T-1/table-fill",
            json={
                "base_release_id": self.r1.id,
                "rows": 2,
                "cols": 2,
                "cells": CELLS,
                "row_totals": ROW_TOTALS,
                "col_totals": COL_TOTALS,
            },
        )
        data = self.client.get("/api/m/T-1/releases").get_json()
        self.assertIsNone(data["releases"][0]["table_fill"])
        fill = data["releases"][1]["table_fill"]
        self.assertEqual(fill["filled"], [["14", "26"], ["4", "5"]])
        self.assertTrue(fill["csv_url"].endswith("/r/2/table.csv"))

    def test_csv_404_without_fill_and_for_unknown_manuscript(self):
        self.assertEqual(self.client.get("/m/T-1/r/1/table.csv").status_code, 404)
        self.assertEqual(self.client.get("/m/NOPE/table.csv").status_code, 404)

    def test_history_release_page_shows_fill_and_form_only_on_current(self):
        self.client.post("/m/T-1/table-fill", data=self.form_payload())
        # 含补字的第 2 版即现行版：展示补字结果与补字表单。
        page = self.client.get("/m/T-1?release=2").get_data(as_text=True)
        self.assertIn("本版含统计表补字", page)
        self.assertIn(">14<", page)  # 格内数字按行列顺序渲染
        self.assertIn(">26<", page)
        self.assertIn("下载第 2 版 CSV", page)
        self.assertIn('id="table-fill-form"', page)
        # 历史第 1 版没有补字记录，也不出现补字表单。
        old = self.client.get("/m/T-1?release=1").get_data(as_text=True)
        self.assertNotIn('id="table-fill-form"', old)
        self.assertNotIn("本版含统计表补字", old)


if __name__ == "__main__":
    unittest.main()
