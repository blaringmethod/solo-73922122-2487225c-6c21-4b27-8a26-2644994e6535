"""End-to-end walkthrough against a running server.

Usage: python tests/walkthrough.py http://127.0.0.1:8000

It drives the same HTML forms a reviewer uses: import boxes, proofread twice,
look up the scan source, try a stale submission, and read a historical release.
Every assertion prints its evidence.
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
# The walkthrough writes real releases; a per-run code keeps reruns clean.
CODE = f"WALKTHROUGH-{uuid.uuid4().hex[:8]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def request(method: str, path: str, data: dict | None = None, json_body=None):
    url = BASE + path
    headers = {}
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
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
    if not match:
        raise SystemExit("no body div in page")
    text = re.sub(r"<[^>]+>", "", match.group(1))
    return html.unescape(text)


def base_release_id(page: str) -> int:
    match = re.search(r'name="base_release_id" value="(\d+)"', page)
    if not match:
        raise SystemExit("no base_release_id input (is this the current release?)")
    return int(match.group(1))


def lookup_rows(page: str) -> list[dict]:
    section = page.split("<h3>", 1)[-1]
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", section, re.S):
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        if len(cells) >= 6 and cells[0].isdigit():
            rows.append(
                {
                    "page_no": int(cells[0]),
                    "box_no": int(cells[1]),
                    "rect": cells[2],
                    "intervals": " ".join(cells[3].split()),
                    "body_ranges": " ".join(cells[4].split()),
                    "box_text": cells[5],
                }
            )
    return rows


boxes = json.loads(Path("sample/boxes.json").read_text(encoding="utf-8"))
expected_initial = "".join(b["text"] for b in boxes)
box_by_key = {(b["page_no"], b["box_no"]): b for b in boxes}


def box_interval(page_no: int, box_no: int) -> str:
    return f"[0, {len(box_by_key[(page_no, box_no)]['text'])})"


def box_offset(page_no: int, box_no: int) -> int:
    total = 0
    for box in boxes:
        if (box["page_no"], box["box_no"]) == (page_no, box_no):
            return total
        total += len(box["text"])
    raise KeyError((page_no, box_no))


box3_offset = box_offset(1, 3)

print(f"== target {BASE} ==")

status, _, page = request("GET", "/")
check("稿件目录可访问", status == 200, f"HTTP {status}")

# 1. Import through the HTML form.
status, headers, page = request(
    "POST",
    "/import",
    data={
        "code": CODE,
        "title": "申报·本埠新闻（试走）",
        "boxes": json.dumps(boxes, ensure_ascii=False),
    },
)
check("导入表单提交后重定向到稿件页", status in (302, 303), f"HTTP {status} -> {headers.get('Location')}")
status, _, page = request("GET", f"/m/{CODE}")
check("稿件页可打开", status == 200, f"HTTP {status}")
check("初稿按阅读顺序拼合各框", body_of(page) == expected_initial, body_of(page))
check("初稿显示为第 1 版", "第 1 版" in page)

# 2. Revise: 議决 -> 議決 (OCR 误字).
body = expected_initial
idx = body.index("决")
base = base_release_id(page)
status, headers, page = request(
    "POST",
    f"/m/{CODE}/revise",
    data={
        "base_release_id": base,
        "start": idx,
        "end": idx + 1,
        "replacement": "決",
        "note": "据原报影印，改「决」为「決」",
    },
)
check("第一次校订后重定向", status in (302, 303), f"HTTP {status}")
status, _, page = request("GET", headers["Location"])
body2 = body_of(page)
check("正文只改了一个字", body2 == body[:idx] + "決" + body[idx + 1 :], body2)
check("新版本号为第 2 版", "第 2 版" in page)

# 3. Look up the changed character in version 2.
status, _, lookup_page = request(
    "GET", f"/m/{CODE}?release=2&lookup_start={idx}&lookup_end={idx + 1}"
)
rows = lookup_rows(lookup_page)
check("查源命中一个框", len(rows) == 1, repr(rows))
check("查源页号为 1、框号为 3、坐标为原框坐标",
      (rows[0]["page_no"], rows[0]["box_no"], rows[0]["rect"]) == (1, 3, "72, 240, 420, 76"),
      repr(rows[0]))
# 第 2 版的「決」是换入字，继承整个选区的原框区间 [0,15)。
check(
    "换入字继承该框完整来源区间",
    box_interval(1, 3) in rows[0]["intervals"],
    repr(rows[0]["intervals"]),
)

# 4. Revise the replacement again: 「決」 -> 「决議」. It must inherit the
#    original box interval rather than forget it.
base2 = base_release_id(page)
status, headers, page = request(
    "POST",
    f"/m/{CODE}/revise",
    data={
        "base_release_id": base2,
        "start": idx,
        "end": idx + 1,
        "replacement": "决議",
        "note": "再改：补「議」字",
    },
)
check("第二次校订后重定向", status in (302, 303), f"HTTP {status}")
status, _, page = request("GET", headers["Location"])
body3 = body_of(page)
check("再次改动的字仍在原位置", body3 == body[:idx] + "决議" + body[idx + 1 :], body3)
check("版本号为第 3 版", "第 3 版" in page)
status, _, lookup_page = request(
    "GET", f"/m/{CODE}?release=3&lookup_start={idx}&lookup_end={idx + 2}"
)
rows = lookup_rows(lookup_page)
check("改后两字共用同一原框来源", len(rows) == 1 and rows[0]["box_no"] == 3, repr(rows))
check(
    "两个新字都继承原框的完整区间",
    box_interval(1, 3) in rows[0]["intervals"],
    rows[0]["intervals"],
)
check("两字对应正文区间连续", rows[0]["body_ranges"] == f"[{idx}, {idx + 2})", rows[0]["body_ranges"])

# 5. Stale submission based on release 1 must be rejected and change nothing.
status, _, page = request(
    "POST",
    f"/m/{CODE}/revise",
    data={"base_release_id": base, "start": 0, "end": 1, "replacement": "改"},
)
check("基于旧版的校订被拒绝", status == 409, f"HTTP {status}")
check("拒绝信息指出现行版", "现行版为第 3 版" in page)
status, _, page = request("GET", f"/m/{CODE}")
check("被拒后现行版仍是第 3 版且正文未变", "第 3 版" in page and body_of(page) == body3)

# 6. Look up in a historical release without replaying edits.
status, _, historical = request(
    "GET", f"/m/{CODE}?release=1&lookup_start={idx}&lookup_end={idx + 1}"
)
check("历史版页面可打开", status == 200)
check("历史版正文为初稿", body_of(historical) == expected_initial)
rows = lookup_rows(historical)
check("历史版查源仍指向页 1 框 3", len(rows) == 1 and rows[0]["rect"] == "72, 240, 420, 76", repr(rows))
check("历史版未改字给出精确框内区间",
      rows[0]["intervals"] == f"[{idx - box3_offset}, {idx - box3_offset + 1})",
      rows[0]["intervals"])
check("历史版不允许提交校订", 'name="base_release_id"' not in historical)

# 7. Delete a span and check neighbouring provenance survives.
base3 = base_release_id(page)
target = body3.index("刻不容緩")
status, headers, page = request(
    "POST",
    f"/m/{CODE}/revise",
    data={"base_release_id": base3, "start": target, "end": target + 4, "replacement": "", "note": "删去误衍四字"},
)
check("删除操作后重定向", status in (302, 303))
status, _, page = request("GET", headers["Location"])
body4 = body_of(page)
check("删除后正文少四字", body4 == body3[:target] + body3[target + 4 :], body4)
status, _, lookup_page = request(
    "GET", f"/m/{CODE}?release=4&lookup_start={target}&lookup_end={target + 3}"
)
rows = lookup_rows(lookup_page)
check(
    "删除处之后的字沿用页 2 框 1 来源",
    len(rows) == 1 and (rows[0]["page_no"], rows[0]["box_no"]) == (2, 1),
    repr(rows),
)
check(
    "其框内区间是原框中的精确三段",
    rows[0]["intervals"] == "[12, 15)",
    rows[0]["intervals"],
)

# 8. A three-character lookup straddling the box 2 / box 3 boundary reports
#    both boxes.
cross_start = box3_offset - 1
status, _, cross_page = request(
    "GET", f"/m/{CODE}?release=1&lookup_start={cross_start}&lookup_end={cross_start + 3}"
)
cross_rows = lookup_rows(cross_page)
check(
    "跨框查源返回页1框2与页1框3",
    [(r["page_no"], r["box_no"]) for r in cross_rows] == [(1, 2), (1, 3)],
    repr([(r["page_no"], r["box_no"]) for r in cross_rows]),
)

# 9. API view of the same data.
status, _, text = request("GET", f"/api/m/{CODE}/releases")
releases = json.loads(text)["releases"]
check("API 列出全部 4 版", len(releases) == 4, str([r["release_no"] for r in releases]))
check("API 各版正文与页面一致", releases[3]["body"] == body4)
status, _, text = request("GET", f"/api/m/{CODE}/lookup?release=1&start={cross_start}&end={cross_start + 3}")
payload = json.loads(text)
check(
    "API 跨框查源含两框及坐标",
    [s["box_no"] for s in payload["sources"]] == [2, 3]
    and payload["sources"][0]["rect"] == {"x": 72, "y": 138, "w": 420, "h": 76},
    json.dumps(payload["sources"], ensure_ascii=False),
)

print("\nWALKTHROUGH_OK")
print("final body:", body4)
