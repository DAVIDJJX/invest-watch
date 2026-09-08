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
