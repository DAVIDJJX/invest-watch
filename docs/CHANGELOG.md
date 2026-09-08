# CHANGELOG — 停點 7：外部精準觸發 ＋ 30 分鐘更新 ＋ 四時段報告

每個子階段結束時追加一段：日期、改了什麼、怎麼驗證的（測試名稱與結果）、怎麼退回。
標籤 `stop7-1` ～ `stop7-4` 各對應一個子階段的 commit。

退回方式（通用）：
```bash
git log --oneline stop7-1 -1        # 看該標籤指到哪個 commit
git checkout stop7-1                # 只是看看（detached）
git revert --no-edit stop7-1..HEAD  # 把 stop7-1 之後的全部改動反轉成新 commit（main 已公開時用這個）
```

---

## 2026-09-08 · 7-1 時段與結束碼

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/fetch_data.py` | 刪掉用時間猜時段的函式；`--slot` 必填，選項 `light / morning / midmorning / close / review / manual`；`--source local` 只接受 `slot=light`（否則結束碼 2、不連網）；結束碼改成 0 全成功／2 部分失敗（印 `::warning::`）／1 程式壞掉；`merge_points` 加「同一天低等級不能蓋高等級」與「定案點清掉 provisional」；Yahoo 解析拆成 `parse_yahoo_chart()`：日期用交易所當地日期（`gmtoffset`），只有進行中的 bar 標 `provisional`；證交所即時價 13:30 前標 `intraday`＋`provisional`、13:30 後標 `close-realtime`；上櫃在完整更新時抓 Yahoo 5d 當定案來源 |
| `scripts/merge_latest.py` | `slot / slotLabel / mode` 只取雲端分片（雲端不見 → `unknown`／「（雲端尚未回報時段）」）；`updatedAt` 仍取兩邊較新；新增「暫定點留過夜（日期 ≤ 前天）」掃描，印 `::warning::`；台銀來源不掃 |
| `scripts/publish.py` | `--rebuild` 接受新時段名、拒絕 `midday`；`report:manual` 的擁有清單改成 `data/report-manual.json` |
| `scripts/dispatch_args.py` | 新增。workflow 第一步：`schedule` 一律 light/light；`workflow_dispatch` 檢查 `slot=light ⇔ mode=light`、其餘 ⇔ `mode=full`，不合法結束碼 2 並印 `::error::` |
| `.github/workflows/update-data.yml` | `inputs.mode`／`inputs.slot` 必填；備援 cron 併成一行（`41 */3 * * *`），只做 light、永遠不產報告；記錄 dispatch→開跑延遲（需 `actions: read`）；抓取步驟結束碼 2 不紅燈、其他非零紅燈；report 只在 `mode=full` 跑；publish 依 mode 決定 `--rebuild` |
| `scripts/update_local.ps1` | 永遠傳 `--slot light`；收到 `-Slot` 只寫 log 忽略（Task Scheduler 的三個工作不用改）；mode 不變 |
| `scripts/test_slots.py` | 新增 27 條 |
| `scripts/test_publish.py` | 新時段名、`report-manual.json`、拒絕 `midday` |

**怎麼驗證的**

- 單元測試 **93 條全綠**（既有 66 ＋ 新 27）：`python -m unittest discover -s scripts -p "test_*.py"`
- 突變對照組（改壞 → 只跑 `test_slots.py` → 必須紅 → 還原），五組全部抓到：

  | 改壞的方式 | 結果 |
  |---|---|
  | 把時段改回用時間猜（`--slot` 選填＋猜） | 2 條紅 |
  | 拿掉「本機只能 light」的檢查 | 1 條紅 |
  | 拿掉同一天的等級檢查（低等級可以蓋高等級） | 2 條紅 |
  | Yahoo 日期改回 UTC（不看 `gmtoffset`） | 2 條紅（第一版測試挑的時間戳在 UTC 與美東同一天、分辨不出來，已改成 02:00 UTC = 前一天 22:00 美東） |
  | latest.json 的時段改回取 runAt 較新的分片 | 2 條紅 |

- 沙盒（複製一份、把 nvda 代號改壞）跑 `--source cloud --slot manual --only nvda` → 結束碼 **2**、stdout 有 `::warning::這一輪有 1 項抓取失敗：nvda`
- `grep -rn guess_slot` 整個 repo：**沒有**
- 競態實測 `scripts/test_race_recovery.py`：全部通過（publish.py 有改，所以重跑）
- 相容性 `scripts/compat_check_latest.py`：合格
- `import probe_bot`：OK（停點 6 的量測沒被弄壞）
- 本機 light 路徑實跑 `fetch_data.py --source local --slot light --light` → 結束碼 0、分片 slot=light；接著 merge → latest.json 的 slot 取雲端分片的值（`manual`）
- 一次沒有重現的觀察：某一次實跑後 latest.json 印出 `slot=morning`，雲端分片明明是 `manual`。之後重跑兩次都是 `manual`，程式碼裡找不到能產生 `morning` 的路徑，相關單元測試與突變都綠。照實記在這裡，不當作沒發生。

**怎麼退回**

```bash
git revert --no-edit stop7-1        # 反轉 7-1 這一個 commit
```

---

## 2026-09-09 · 7-2 報告與前端

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/report.py` | 四個時段各一支：`morning`（不變）、`midmorning`（沿用舊 midday 的內容）、`close`（新：短的「台股收盤快報」，只有收盤價與跟晨報比較）、`review`（沿用舊 close 的骨架再加「今晚觀察」，只列事實）；每份加 `producedBy`（host／event／runId）；`manual` 寫 `data/report-manual.json`，不碰 `report-latest.json`，但照樣進 archive；`update_index` 排序與 `previous_close_report` 同時認得新舊名字（舊檔案不改名、不重寫歷史）；CLI 拒絕 `midday` |
| `js/history.js` | `SLOT_NAME`／`SLOT_ORDER` 加 `midmorning`、`review`，保留 `midday` 給舊的日子；「缺了哪些時段」依那一天是哪一套判斷（2026-09-09 前是三份、之後是四份），舊的日子不會被說成缺「午前、盤後」 |
| `index.html` | 只改標語那一行：「平日每 30 分鐘更新，09:30／11:30／13:35／15:30 出報告」 |
| `scripts/publish.py` | 報告檔的擁有清單同時列今天與昨天的 archive 路徑（跨午夜的 run 才不會漏掉剛寫的那份） |
| `scripts/test_reports.py` | 新增 10 條（輸入用真實 latest.json、寫入全導到暫存目錄） |
| `scripts/test_publish.py` | 加跨午夜那一條 |

**怎麼驗證的**

- 單元測試 **104 條全綠**
- 突變對照組（改壞 → 只跑 `test_reports.py` → 必須紅 → 還原），四組全部抓到：manual 也去蓋 report-latest（1 紅）、`update_index` 排序表改回只有舊名字（1 紅）、`previous_close_report` 不認得舊的 midday（1 紅）、盤後拿掉今晚觀察（1 紅）
- 競態實測：**第一次跑紅了 3 條**——時間剛好跨午夜，測試開頭算的 TODAY 是 09-08，`publish.py` 內部算擁有清單時已是 09-09，報告檔路徑對不上、工作區留下未提交的檔案（結束碼 3）。過午夜重跑全過。這不只是測試的問題，正式流程也會有同樣的邊角，所以 `publish.py` 改成今天與昨天的路徑都列，並補一條測試釘住
- compat：合格；`import probe_bot`：OK
- 前端（`python -m http.server`＋瀏覽器）：歷史頁 2026-09-08 顯示「收盤／手動更新」、缺「晨報、午盤」（正確，那一天是三份那一套）；2026-09-04 四個分頁齊全、沒有缺席提示；首頁標語已換、13 張卡片與圖表正常；console 零錯誤
- 測試沒有動到真實的報告檔（`git status data/` 零變動）

**怎麼退回**

```bash
git revert --no-edit stop7-1..stop7-2   # 只反轉 7-2
```
