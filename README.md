# 报刊 OCR 校订档案（Flask）

把报刊 OCR 识别框按阅读顺序导入，拼成初稿；校订员以 Unicode 码点区间替换或删除正文；
任何一版正文都能按区间查回扫描原框（页号、框号、矩形坐标、框内区间）。

另有**统计表补字**：档案员录入 2～4 行、2～4 列数据格的码点区间与纸面行列合计，系统按
每个问号补一位数字的约束枚举填法；只有行列合计同时满足且**唯一解**时才一次补齐全部问号、
生成新版并随版保存供数 CSV；无解或多解（列出两组候选）都不发版。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据卷 `archive-data` 保存 SQLite 文件，重建容器不丢版本。

不用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
OCR_DATABASE=./archive.sqlite3 gunicorn --bind 0.0.0.0:8000 ocr_archive.wsgi:app
```

## 试走一遍：导入 → 校订 → 查源

1. 打开首页，填入稿号与标题，把 `sample/boxes.json` 的内容贴进「识别框 JSON」（或上传该文件）。
   提交后得到初稿：各框文字按数组顺序拼接，成为第 1 版。
2. 在正文里选中「决」字（或直接填起点 28、终点 29），换入「決」，写一句校订说明，提交。
   生成第 2 版。
3. 在第 2 版点「用当前选择查源」或填 `[28, 29)` 查源：表格列出「页 1 / 框 3 / 72, 240, 420, 76」，
   框中区间 `[0, 15)` 并标注这是换入字继承来的整段来源。
4. 再把刚改的「決」改成「决議」：新字继承选区原有的框内来源，不会丢失出处。
5. 点「校次」里的第 1 版查看历史快照：未改的「决」查源精确到框内 `[1, 2)`；此页不能提交校订。
   另开一个标签页从第 3 版提交校订，再回旧表单提交，会被 409 拒绝且现行版不变。

脚本化复跑（对运行中的服务）：

```bash
python3 tests/walkthrough.py http://127.0.0.1:8000        # 导入 → 校订 → 查源全流程
python3 tests/table_walkthrough.py http://127.0.0.1:8000  # 统计表补字、CSV、无解/多解/409
```

统计表补字可直接用 `sample/table_boxes.json` 试跑：导入后在稿件页「统计表补字」面板录入

- 数据格区间 `[[[9,11],[12,14]],[[15,16],[17,18]]]`
- 行合计 `40,9`、列合计 `18,31`

唯一解把 `1? 2? 4 5` 补成 `14 26 4 5`，第 2 版即可下载 CSV（`14,26` / `4,5`）。

## 关键约定

- **码点**：正文偏移与框内区间都是从 0 起的 Unicode 码点，半开区间 `[start, end)`；
  浏览器选区由前端换算成码点后提交，服务端不猜。
- **识别框**：`page_no`（页号）、`box_no`（框号）、`rect: [x, y, w, h]`（也接受 `x/y/w/h`）、`text`；
  数组顺序即阅读顺序，同一页内框号唯一。
- **初稿**：所有框文字按阅读顺序直接拼接，无分隔符。
- **校订**：每次只改一段非空正文。换入的每个字继承选区内出现过的**全部**框内来源（并集）；
  之后改这些字，仍按它们已有的来源继续继承。未改的字沿用原出处的来源集合行。
  换入字在查源中标为 `exact: false`，其框内区间是继承来的整段；未改的原文按字符线性对应，
  查源精确到框内的 `[start, end)`（`exact: true`）。
- **同源合并**：正文按「来源集合相同」切成极大连续段存储，连续同源的字只占一份来源集合，
  来源集合内容寻址、跨版共享；未改字即使正文位置移动也不复制来源记录，只在 `body_span`
  里记一个 `map_start` 让查源仍能精确回指框内位置。
- **发版**：正文与来源在同一 SQLite 事务内写入一个新 `release`；旧版是完整快照，读取不需要重放校订。
- **只接受现行版**：提交必须带 `base_release_id`；若写事务开启时它已不是现行版，返回 409，
  事务回滚，`current_release_id` 与所有表都不变。
- **统计表补字**：每格正文只允许 `0～9` 与半角 `?`，每格至多一个问号、全表 1～6 个；
  数据格区间沿用半开写法且互不重叠。回溯枚举问号的 10 种取值（带行列上界剪枝，最多保留两组解），
  无解与多解均不发版。唯一解在一个 `BEGIN IMMEDIATE` 事务内逐字符等长替换全部问号：
  新数字继承对应问号所在同源段的整段来源（`exact: false`），未改文字沿用原出处、仍精确到框内区间；
  表规格（区间、行列合计、补后数据格）随新版写入 `table_fill`，供数 CSV 可按版号重新生成。
  提交时若所据版本已不是现行版，或事务中任何写入失败，补字与 CSV 一并回滚、均不生效。
  核算期间普通校订不受影响，补字之后仍可继续校订。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `manuscript` | 稿号、标题、现行版指针 |
| `box` | 识别框：页号、框号、坐标、文字、阅读顺序 |
| `release` | 不可变版本：`body` 全文、父版本、校订说明 |
| `body_span` | 正文的极大同源段 `[body_start, body_end)` → 来源集合；`linear`/`map_start` 记录原文与框内字符的一一对应 |
| `source_set` / `source_entry` | 跨版共享的来源集合，条目为（框, 框内区间 `[start, end)`） |
| `table_fill` | 统计表补字随新版保存的表规格：行列数、各格码点区间、行列合计、补后数据格、问号数 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 稿件目录 + 导入表单 |
| POST | `/import` | 表单导入（也接受 JSON 文件上传） |
| GET | `/m/<code>?release=N` | 指定版正文与校次（只读展示旧版） |
| POST | `/m/<code>/revise` | 表单校订：`base_release_id,start,end,replacement,note` |
| GET | `/m/<code>?lookup_start=&lookup_end=[&page=]` | 表单查源 |
| POST | `/m/<code>/table-fill` | 表单统计表补字：`base_release_id,rows,cols,cells(JSON),row_total,col_total,note`；无解/多解回页面不发版 |
| GET | `/m/<code>/r/<release_no>/table.csv` | 下载该版供数 CSV（数据格按原表行列顺序）；无补字记录返回 404 |
| GET | `/m/<code>/table.csv` | 现行版的供数 CSV |
| POST | `/api/import` | JSON：`{code,title,boxes:[...]}` |
| POST | `/api/m/<code>/revise` | JSON：`{base_release_id,start,end,replacement,note}`；过期版返回 409 |
| POST | `/api/m/<code>/table-fill` | JSON：`{base_release_id,rows,cols,cells,row_totals,col_totals,note}`；唯一解发版，无解/多解回 `status` 与至多两组候选且 `published:false`，过期版 409 |
| GET | `/api/m/<code>/releases` | 全部版本的正文与说明，含补字版的 `table_fill` 规格与 `csv_url` |
| GET | `/api/m/<code>/lookup?start=&end=[&release=][&page=]` | JSON 查源 |
| GET | `/healthz` | 健康检查 |

## 测试

```bash
python3 -m unittest discover -s tests        # 70 个用例：领域逻辑、补字求解/发版、HTTP、并发过期、码点
python3 tests/walkthrough.py http://...      # 对真实服务走表单流程
python3 tests/table_walkthrough.py http://...# 统计表补字端到端（含 CSV 与无解/多解/409）
python3 tests/concurrency.py http://...      # 并发提交/重复导入竞态
node tests/selection_test.js /tmp/page.html ocr_archive/static/app.js
                                             # 浏览器选区→码点映射（需先跑 tests/render_for_js.py）
```
