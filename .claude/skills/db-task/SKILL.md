---
name: db-task
description: 從一個 task 資料夾 (例如 tasks/260826) 整理出後台調整的 CSV 動作清單 (刪除資料夾 / 換相機位置)。使用時機 - 使用者輸入 /db-task <folder>，或是拿到客戶的調整需求檔案 (TSV 表格 / 中文文字) 要整理成待辦清單。
---

# db-task

讀一個 task 資料夾裡的需求檔案，查資料庫，輸出 `<folder>/action-list.csv`:

```
index, folder_name, deployment_journal_id, action, swap_from_deployment, swap_to_deployment
```

- `action`: `delete` 或 `swap`
- `swap_from_deployment` / `swap_to_deployment`: `{deployment_name} [{deployment_id}]`，delete 兩欄留空
- `folder_name`: 用資料庫裡的寫法 (表格可能大小寫不同)

**鐵則: 每一個 `deployment_journal_id` 和 `deployment_id` 都必須是查詢結果裡真的有的
數字。查不到就列進問題清單，絕對不要用推的、不要照編號規則自己算、不要沿用別的
資料夾的 id。這個 skill 只產生清單，不執行刪除或搬移。**

## 1. 決定資料夾

沒給就用 `tasks/` 底下最新的 `YYMMDD` 資料夾，並說明你選了哪一個。`ls` 看有哪些檔案。

## 2. 讀檔，整理出 jobs

每個資料夾一筆 job: `folder_name` / `action` / `swap_from` (傳錯相機編號) / `swap_to` (正確相機編號)。

**TSV 表格** (欄位: 資料夾名稱 / 正確相機編號 / 傳錯相機編號 / 上傳者 / 備註):
用欄位名稱對，不要用位置 (不同表格欄數不一樣)。正確相機編號 是 `刪除資料夾` → `delete`；
其他有填 → `swap`，`swap_to` = 正確相機編號，`swap_from` = 傳錯相機編號。兩欄都空 → 列問題。

**自由文字** (`.txt` / `.md`): 「刪除資料夾」→ delete；「傳錯相機 / 換相機位置」→ swap。
相機編號和資料夾名稱照抄，不要補零、不要改大小寫。看不出來要做什麼、或需求含糊
(例如只說「某某分署的資料有問題」) 就不要編，列進問題清單問使用者。

**「修改影像日期年份」之類的其他需求沒有對應的 script，不要放進 CSV，直接回報。**

跨檔案同一個 `folder_name`: 指示相同就留一筆；指示衝突 (一邊 delete 一邊 swap、或
`swap_to` 不同) 就兩筆都不放進 CSV，列進問題清單。

表格可能有幾十列，逐列處理，不要略過中間。整理完先核對「讀到幾列 = jobs 幾筆 + 問題幾筆」。

## 3. 查資料庫

一次查完，不要一個資料夾查一次。把 SQL 寫到暫存檔再送進 psql (省掉引號轉義):

```sql
-- query.sql
SELECT dj.id AS journal_id, dj.folder_name, dj.project_id,
       d.id AS deployment_id, d.name AS deployment_name
FROM taicat_deploymentjournal dj
LEFT JOIN taicat_deployment d ON d.id = dj.deployment_id
WHERE lower(dj.folder_name) IN ('nt-ltmm-l09-20260601-20260704', '...')  -- 全部 job 的資料夾
ORDER BY dj.folder_name, dj.id;

SELECT id, name, project_id, deprecated
FROM taicat_deployment
WHERE lower(name) IN ('nt-ltmm-l09', '...')   -- 全部 swap 的 swap_to 相機編號
ORDER BY name, project_id;
```

**一律查 production**，用 `ssh ct-prod`:

```bash
ssh ct-prod "cd camera-trap-server;docker-compose -f compose.yml exec -T postgres psql -U postgres -d cameratrap" < query.sql
```

本機的資料庫是 `make db-dump` 抓下來的舊 dump，最近上傳的資料夾不會有、id 也可能對不上，
**不要拿來產生清單**。ssh 連不上就停下來跟使用者說，不要改用本機資料。

## 4. 組 CSV

對每一筆 job，用查詢結果判斷:

| 情況 | 處理 |
|---|---|
| 查不到這個 folder_name | 問題: 資料庫找不到 |
| 同一個 folder_name 有多筆 journal | 取 id 最大的那筆 (跟 `delete_upload_folder.py` 一致)，列入注意 |
| delete | 直接寫一列，swap 兩欄留空 |
| swap: journal 的 deployment 是 NULL | 問題: 沒有 deployment 無法 swap |
| swap: `swap_to` 在該 journal 的 project_id 內找不到 / 找到多筆 | 問題: 需要人工指定 |
| swap: 目標 = 目前的 deployment | 問題: 已經在正確位置，不需要 swap (表格備註寫「照片先前已修正過位置」的通常是這種) |
| swap: 目前的 deployment name ≠ 表格的傳錯相機編號 | 問題: 現況與表格不符，先確認 |
| swap: 目標 deployment `deprecated = true` | 照樣寫，列入注意 |

`swap_from_deployment` 一律用**資料庫裡 journal 現在的位置**，不是表格上的傳錯相機編號 —
表格那欄只拿來核對有沒有被搬過。

`index` 從 1 連號。CSV 存 UTF-8 (加 BOM，Excel 開中文才不會亂碼)。
問題和注意寫到 `<folder>/action-list-problems.txt`，一行一筆:
`來源檔案:行號<TAB>folder_name<TAB>原因`，依來源檔名 + 行號排序 —
行號要用**數字**比大小，不要用字串排 (不然 `:10` 會排到 `:2` 前面)。

CSV 的 `index` 是重編的流水號，不是來源行號 (被擋掉的不佔號、來源又有好幾個檔)，
兩邊要對照就用 `folder_name`。

## 5. 回報

幾筆 delete / 幾筆 swap、CSV 路徑，以及注意和無法處理的清單。被擋下來的照實說，
讓使用者決定，不要自己想辦法繞過。

## 執行 (第二階段)

使用者說要執行 (例如 `/db-task tasks/260826 執行`) 才做，而且只照 `<folder>/action-list.csv`
跑 — 不要重新解析 TSV、不要重查 id。CSV 上的 `deployment_journal_id` /
`swap_to_deployment` 裡的數字直接用。

### 1. 先存快照

delete 之後 journal 就查不到了，所以動手前先把 CSV 上全部 `deployment_journal_id` 的
整列資料撈下來 (`SELECT *`，欄位以資料庫為準，不要自己列):

```sql
-- snapshot.sql
SELECT * FROM taicat_deploymentjournal WHERE id IN (36443, 36251, ...) ORDER BY id;
```

```bash
ssh ct-prod "cd camera-trap-server;docker-compose -f compose.yml exec -T postgres psql \
  -U postgres -d cameratrap --csv -P pager=off" < snapshot.sql > snapshot.csv
```

csv 格式用 psql 的 `--csv -P pager=off` 參數，**不要**在 SQL 裡寫 `\pset` —
`\pset` 會把 "Pager usage is off." 之類的訊息印到輸出裡，混進 csv。

筆數要跟 CSV 對得起來，少了就先停下來問。

### 2. 一筆一筆跑

照 `index` 順序，一次一筆，**不要包成迴圈一次跑完**。每一筆先 dry run 看過再下手:

```bash
# delete (用 CSV 上的 deployment_journal_id，同名資料夾有多筆時才不會刪錯)
make delete-folder ID=<deployment_journal_id> DRY_RUN=1
make delete-folder ID=<deployment_journal_id>

# swap (--project 和 --to-deployment 用 CSV 上的數字)
ssh ct-prod "cd camera-trap-server;docker-compose -f compose.yml exec -T django \
  python scripts/swap-deployment.py --project <id> --folder <folder_name> --to-deployment <id> --commit"
```

某一筆失敗就記下錯誤訊息、繼續下一筆，不要停掉整批，也不要自己想辦法繞過。

### 3. 驗證

全部跑完後**再查一次資料庫**確認結果，不要拿 script 的輸出當成功:

```sql
SELECT id, folder_name, deployment_id FROM taicat_deploymentjournal WHERE id IN (...) ORDER BY id;
```

- `delete` 成功 = 這個 id 查不到了
- `swap` 成功 = `deployment_id` 等於 CSV 上 `swap_to_deployment` 的數字

### 4. 寫工作紀錄

`<folder>/result.csv`，一筆一列，欄位:

```
<taicat_deploymentjournal 的所有欄位，用快照的內容和順序> , process, is_success, process_time, note, swap_from_deployment, swap_to_deployment
```

- 前面的欄位是**動作前的快照** (delete 的資料才留得下來)
- `process`: `delete` / `swap`
- `is_success`: `true` / `false`，依步驟 3 查回來的結果填，不是依 script 有沒有噴錯
- `process_time`: 執行當下時間，台灣時間 `YYYY-MM-DD HH:MM:SS`
- `note`: 失敗原因；成功留空 (`is_success=false` 沒有原因等於沒紀錄，所以多這一欄)
- `swap_from_deployment` / `swap_to_deployment`: 直接抄 `action-list.csv` 同一筆的值
  (`{name} [{id}]`)，`delete` 兩欄留空。試算表第一列已有這兩欄，`gsheet.py` 依欄名對應，不用改

CSV 存 UTF-8 (加 BOM)。`result.csv` 已經存在就往後接，不要覆蓋 — 重跑失敗的那幾筆時
靠 `process_time` 分辨。

### 5. 同步到 Google Sheet

`result.csv` 寫好之後，把紀錄加到「自動相機系統後台調整紀錄」(工作表1)，
用 `gsheet.py` (只用 python 標準庫 + openssl，不需要 pip):

```bash
python3 -I .claude/skills/db-task/gsheet.py camera-trap-docs-*.json \
  1mPgCmx7ZtyoW1Cau7UGeH0zeG643zNzrL0szfYvwEek append 工作表1 <folder>/result.csv --dry-run
# 看過 skip / append 的清單沒問題，再拿掉 --dry-run 正式寫入
```

- service account 金鑰在 repo 根目錄 `camera-trap-docs-*.json` (已用 `.git/info/exclude`
  擋掉，不要 `git add`、不要印出內容)。找不到金鑰、或回 403/404，就停下來跟使用者說，
  不要自己想辦法繞過 (使用者要先把試算表分享給金鑰裡的 `client_email`，權限要是編輯者)。
- 欄位依試算表第一列的名稱對應: `PROCESS_DATE` = task 資料夾名稱 (例如 `261002`)，
  `who` = `auto` (AI 執行的都填這個)，其餘欄位照 `result.csv`。
- 只會往下新增，不會改既有的列。同一個 `PROCESS_DATE` + `id` + `process` + `is_success` 試算表裡已經有
  就跳過，所以整份 `result.csv` 重送也不會重複。
- 使用者說他已經手動貼過了，就不用再跑。

最後回報: 成功幾筆 / 失敗幾筆、失敗的是哪幾個資料夾和原因、`result.csv` 路徑、
試算表新增了幾列 (跳過幾列)。
