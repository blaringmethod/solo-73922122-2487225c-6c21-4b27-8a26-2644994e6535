"""Concurrency checks against a running server.

Several proofreaders submit from the same base release at the same time:
exactly one write must win, the others must get 409, and the manuscript must
end up with one new release only. Duplicate imports of one 稿号 are also raced.

Usage: python tests/concurrency.py http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
BOXES = [{"page_no": 1, "box_no": 1, "rect": [0, 0, 10, 10], "text": "甲乙丙丁戊"}]


def call(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or "{}")


def get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or "{}")


suffix = uuid.uuid4().hex[:8]
code = f"CONC-{suffix}"
status, data = call("/api/import", {"code": code, "title": "并发", "boxes": BOXES})
assert status == 201, (status, data)
base_id = data["release_id"]
print("base release id:", base_id)

status, probe = call(
    f"/api/m/{code}/revise",
    {"base_release_id": 999_999, "start": 0, "end": 1, "replacement": "x"},
)
print("probe with bogus base id ->", status)
assert status == 404, (status, probe)

results: list[tuple[int, dict]] = []
barrier = threading.Barrier(4)


def worker(index: int) -> None:
    barrier.wait()
    status, body = call(
        f"/api/m/{code}/revise",
        {
            "base_release_id": base_id,
            "start": 0,
            "end": 1,
            "replacement": f"改{index}",
        },
    )
    results.append((status, body))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()

statuses = sorted(s for s, _ in results)
print("statuses:", statuses)
ok = sum(1 for s, _ in results if s == 200)
conflict = sum(1 for s, _ in results if s == 409)
print(f"accepted={ok} conflicts={conflict}")
assert (ok, conflict) == (1, 3), results

status, releases = get(f"/api/m/{code}/releases")
print("release count:", len(releases["releases"]))
assert len(releases["releases"]) == 2, releases
winner = releases["releases"][1]
assert winner["body"].startswith(("改0", "改1", "改2", "改3"))
assert releases["current_release_id"] == winner["release_id"]

dup_code = f"CONC-DUP-{suffix}"
dup_statuses: list[int] = []
dup_barrier = threading.Barrier(4)


def dup_worker() -> None:
    dup_barrier.wait()
    status, _ = call(
        "/api/import", {"code": dup_code, "title": "重复并发", "boxes": BOXES}
    )
    dup_statuses.append(status)


threads = [threading.Thread(target=dup_worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("duplicate import statuses:", sorted(dup_statuses))
assert dup_statuses.count(201) == 1, dup_statuses
assert dup_statuses.count(400) == 3, dup_statuses

print("CONCURRENCY_OK")
