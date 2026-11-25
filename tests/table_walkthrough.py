"""统计表补字端到端走查（对运行中的服务）。

Usage: python tests/table_walkthrough.py http://127.0.0.1:8000

覆盖：
1. 导入 sample/table_boxes.json（正文含两个问号的 2x2 统计表）；
2. 表单录入区间与纸面行列合计，唯一解时补字发版；
3. 新数字继承问号来源（查源 exact=false、整框区间），未改文字沿用原出处；
4. 按新版号下载供数 CSV，数据格按原表行列顺序；
5. 无解、多解均不发版（多解列出两组候选）；
6. 所据版本过期返回 409，补字与 CSV 均不生效；
7. 核算期间普通校订仍可提交。
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
CODE = f"TABLE-{uuid.uuid4().hex[:8]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def request(method: str, path: str, data: dict | None = None, json_body=None):
    headers = {}
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS  " if condition else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        raise SystemExit(f"walkthrough failed at: {label}")


def body_of(page: str) -> str:
    match = re.search(r'<div id="body"[^>]*>(.*?)</div>', page, re.S)
    text = re.sub(r"<[^>]+>", "", match.group(1))
    return html.unescape(text)


def base_release_id(page: str) -> int:
    match = re.search(r'name="base_release_id" value="(\d+)"', page)
    return int(match.group(1))


boxes = json.loads(Path("sample/table_boxes.json").read_text(encoding="utf-8"))
initial_body = "".join(b["text"] for b in boxes)
# 数据格码点区间（跳过空格），与正文 "本埠各業商號統計表1? 2? 4 5 " 对齐。
cells = [[[9, 11], [12, 14]], [[15, 16], [17, 18]]]
row_totals, col_totals = [40, 9], [18, 31]

print(f"== target {BASE} ==")

# 1. 导入含统计表问号的识别框。
status, headers, _ = request(
    "POST",
    "/import",
    data={
        "code": CODE,
        "title": "本埠各業商號統計表（补字走查）",
        "boxes": json.dumps(boxes, ensure_ascii=False),
    },
)
check("导入后重定向", status in (302, 303), f"HTTP {status}")
status, _, page = request("GET", f"/m/{CODE}")
check("稿件页可打开且初稿含问号", status == 200 and "?" in body_of(page), body_of(page))
check("初稿正文与识别框拼接一致", body_of(page) == initial_body, body_of(page))
base_id = base_release_id(page)

# 2. 唯一解补字（HTML 表单）。
status, headers, _ = request(
    "POST",
    f"/m/{CODE}/table-fill",
    data={
        "base_release_id": base_id,
        "rows": "2",
        "cols": "2",
        "cells": json.dumps(cells),
        "row_total": ",".join(str(v) for v in row_totals),
        "col_total": ",".join(str(v) for v in col_totals),
        "note": "据纸面行列合计补齐",
    },
)
check("唯一解补字后重定向到新版", status in (302, 303), f"HTTP {status} -> {headers.get('Location')}")
status, _, page = request("GET", headers["Location"])
filled_body = body_of(page)
check("两个问号一次补齐", filled_body == "本埠各業商號統計表14 26 4 5 ", filled_body)
check("生成第 2 版且标注统计表补字", "第 2 版" in page and "统计表补字" in page)

# 3. 来源核对：新数字继承问号所在框的整段区间；未改字精确回指。
status, _, text = request("GET", f"/api/m/{CODE}/lookup?release=2&start=10&end=11")
payload = json.loads(text)
src = payload["sources"][0]
check(
    "新数字继承框2整段来源且非精确",
    (src["page_no"], src["box_no"]) == (1, 2)
    and src["box_intervals"] == [{"start": 0, "end": 6}]
    and src["exact"] is False,
    json.dumps(src, ensure_ascii=False),
)
status, _, text = request("GET", f"/api/m/{CODE}/lookup?release=2&start=9&end=10")
src = json.loads(text)["sources"][0]
check(
    "未改的「1」沿用原出处、框内区间精确",
    src["box_intervals"] == [{"start": 0, "end": 1}] and src["exact"] is True,
    json.dumps(src, ensure_ascii=False),
)

# 4. 按新版号下载 CSV。
status, headers, csv_text = request("GET", f"/m/{CODE}/r/2/table.csv")
check("第 2 版 CSV 可下载", status == 200 and "attachment" in headers.get("Content-Disposition", ""))
check("CSV 数据格按原表行列顺序", csv_text == "14,26\n4,5\n", repr(csv_text))
status, _, csv_current = request("GET", f"/m/{CODE}/table.csv")
check("现行版快捷 CSV 与版号 CSV 一致", csv_current == csv_text)
status, _, _ = request("GET", f"/m/{CODE}/r/1/table.csv")
check("无补字的旧版没有 CSV", status == 404, f"HTTP {status}")

# 5. 无解：另建一份仍带问号的稿件，总和一致但单位数无法满足，不发版。
no_code = f"TABLE-N-{uuid.uuid4().hex[:6]}"
request(
    "POST",
    "/api/import",
    json_body={"code": no_code, "title": "无解统计表", "boxes": boxes},
)
_, _, no_page = request("GET", f"/m/{no_code}")
status, _, page = request(
    "POST",
    f"/m/{no_code}/table-fill",
    data={
        "base_release_id": base_release_id(no_page),
        "rows": "2",
        "cols": "2",
        "cells": json.dumps(cells),
        "row_total": "99,9",
        "col_total": "100,8",
    },
)
check("无解停留当前页且提示无解", status == 200 and "无解" in page, f"HTTP {status}")
status, _, text = request("GET", f"/api/m/{no_code}/releases")
check("无解未产生新版", len(json.loads(text)["releases"]) == 1)

# 6. 多解：另建全问号稿件，列出两组候选且不发版。
q_code = f"TABLE-Q-{uuid.uuid4().hex[:6]}"
request(
    "POST",
    "/api/import",
    json_body={
        "code": q_code,
        "title": "全问号表",
        "boxes": [{"page_no": 1, "box_no": 1, "rect": [0, 0, 10, 10], "text": "????"}],
    },
)
_, _, q_page = request("GET", f"/m/{q_code}")
q_base = base_release_id(q_page)
status, _, page = request(
    "POST",
    f"/m/{q_code}/table-fill",
    data={
        "base_release_id": q_base,
        "rows": "2",
        "cols": "2",
        "cells": json.dumps([[[0, 1], [1, 2]], [[2, 3], [3, 4]]]),
        "row_total": "1,1",
        "col_total": "1,1",
    },
)
check("多解提示并列两组候选", status == 200 and "多解" in page and page.count("候选填法") == 2)
_, _, text = request("GET", f"/api/m/{q_code}/releases")
check("多解未产生新版", len(json.loads(text)["releases"]) == 1)

# 7. 过期版本：另建一份带问号稿件，用普通校订把现行版推进到第 2 版（问号原样
#    保留），再持第 1 版 id 提交补字，应被 409 拒绝且补字、CSV 均不生效。
stale_code = f"TABLE-S-{uuid.uuid4().hex[:6]}"
request(
    "POST",
    "/api/import",
    json_body={"code": stale_code, "title": "过期补字", "boxes": boxes},
)
_, _, stale_page = request("GET", f"/m/{stale_code}")
stale_base = base_release_id(stale_page)
status, _, _ = request(
    "POST",
    f"/m/{stale_code}/revise",
    data={"base_release_id": stale_base, "start": 0, "end": 1, "replacement": "全", "note": "先推进版本"},
)
check("对照稿件先行校订到第 2 版", status in (302, 303), f"HTTP {status}")
status, _, text = request(
    "POST",
    f"/api/m/{stale_code}/table-fill",
    json_body={
        "base_release_id": stale_base,
        "rows": 2,
        "cols": 2,
        "cells": cells,
        "row_totals": row_totals,
        "col_totals": col_totals,
    },
)
check("过期版补字返回 409", status == 409 and json.loads(text)["code"] == "stale_release", text)
_, _, text = request("GET", f"/api/m/{stale_code}/releases")
stale_releases = json.loads(text)["releases"]
check("409 后对照稿仍只有两版且无补字记录",
      len(stale_releases) == 2 and all(r["table_fill"] is None for r in stale_releases))
status, _, _ = request("GET", f"/m/{stale_code}/table.csv")
check("409 后没有 CSV 可下载", status == 404, f"HTTP {status}")
# 8. 核算期间普通校订仍可用：在已补字的稿件上再做一笔等长校订（第 3 版），
#    补出的数字保留，已发版 CSV 不受后续校订影响。
_, _, cur_page = request("GET", f"/m/{CODE}")
status, headers, _ = request(
    "POST",
    f"/m/{CODE}/revise",
    data={"base_release_id": base_release_id(cur_page), "start": 0, "end": 1,
          "replacement": "全", "note": "核算期间校订标题"},
)
check("核算期间普通校订仍可提交", status in (302, 303), f"HTTP {status}")
status, _, page = request("GET", headers["Location"])
check("校订后补出的数字仍在正文中", "14" in body_of(page))
_, _, csv_text = request("GET", f"/m/{CODE}/r/2/table.csv")
check("已发版 CSV 不受后续校订影响", csv_text == "14,26\n4,5\n")

# 9. releases API 带 table_fill 与 csv_url。
_, _, text = request("GET", f"/api/m/{CODE}/releases")
data = json.loads(text)
fill = data["releases"][1]["table_fill"]
check(
    "API 回放补字规格与下载地址",
    fill["filled"] == [["14", "26"], ["4", "5"]]
    and fill["row_totals"] == row_totals
    and fill["col_totals"] == col_totals
    and fill["csv_url"].endswith("/r/2/table.csv"),
    json.dumps(fill, ensure_ascii=False),
)

print("\nTABLE_WALKTHROUGH_OK")
print("final body:", body_of(page))
