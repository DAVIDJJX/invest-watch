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
- 一次沒有重現的觀察：某一次實跑後 latest.json 印出 `slot=morning`，雲端分片明明是 `manual`。之後重跑兩次都是 `manual`。
  **2026-09-09 已查明原因（沙盒重現成功）**：突變對照組「把時段改回用時間猜」讓 `--slot` 變成選填、猜出 `morning`；
  `test_slot_is_required` 那條測試是把 `fetch_data.py --source cloud` 當子程序跑的，檢查一被拿掉它就**真的去抓了一次雲端資料**，
  把真實的 `data/sources/cloud.json` 寫成 `slot=morning`；之後 `git checkout -- data/` 才還原。不是 merge 的邏輯有問題，是測試在突變之下有副作用。
  已修：那兩條子程序測試多帶一個不存在的 `--only`，就算前面的檢查被拿掉，「--only 指到範圍外」也會在連網前擋下來（見收尾 A）。

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

---

## 2026-09-09 · 7-3 schedule.json 與 freshness

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `data/schedule.json` | 回歸描述現實的排程（cron-job.org 的時間表）：`cloud.full` 平日 09:30／11:30／13:35／15:30；`cloud.light` 兩個時窗——平日 08:10～17:40 每 30 分（:10 與 :40）、週末 08:10～20:10 每 2 小時；`reports` 改成四個 slot；`graceMinutes` 30。原本「15:20 怎麼來的」那一大段改成「已改由外部精準觸發；量測資料見 git 歷史 dcf2ff4」。備援 cron 不在這張表裡（它只做 light、不產報告，不是正式排程） |
| `scripts/schedule_util.py` | `light` 可以是一串時窗（舊的單一物件寫法仍相容）；雲端因此預設 cadence 變成 light |
| `scripts/test_schedule_util.py` | 假排程對齊新現實；新增 7-F 四條：平日整天每 10 分鐘看一次零誤判、週末零誤判、外部觸發停 3 小時 → stale、單一報告 run 漏跑不算 freshness 問題（那是 watchdog 的事） |

**怎麼驗證的**

- 單元測試 **110 條全綠**
- 7-F：模擬「每個排定時間點都真的跑到、資料 +40 秒落地」，平日與週末每 10 分鐘看一次，**零誤判**；模擬 11:00～14:00 沒有任何觸發，13:30 判 **stale**、14:10 那一輪跑到後恢復
- 突變對照組（改壞 → 只跑 `test_schedule_util.py` → 必須紅 → 還原），四組全部抓到：只看第一個 light 時窗（2 紅）、light 不看自己的 days（8 紅）、舊寫法不再相容（10 紅）、寬限放大到 4 小時（7 紅）
- 一條測試改過斷言：原本以為「09:30 報告 run 漏跑 → 09:35 會 stale」，實際上 09:10 的 light 已更新過現價、資料在寬限內，說 fresh 才是對的。改成釘住這個語意（報告缺席由 watchdog 抓，不是 freshness）
- 競態實測全過、compat 合格、`import probe_bot` OK
- 用新排程對真實資料合併一次（週三 00:13）：13 項 fresh，`lastDue` 週二 17:40、下一次週三 08:10，合理

**怎麼退回**

```bash
git revert --no-edit stop7-2..stop7-3   # 只反轉 7-3
```

---

## 2026-09-09 · 7-4 文件與線上驗證

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `docs/scheduler-setup.md` | 新增。cron-job.org 六個 job 的名稱／URL／方法／三個 header／body／時區／排程、點哪裡打什麼、怎麼驗證（單一 job、兩小時觀察 7-H、隔天四份 7-I）、怎麼停用。文件裡沒有真鑰匙 |
| `README.md` | 「自己動手跑」的範例補上必填的 `--slot`（本機只能 light）；進度區加停點 7；維運區加「排程在哪裡、怎麼確認它有在跑」 |
| `docs/CHANGELOG.md` | 這份 |

**線上驗證（在分支 `feat/external-dispatch` 上用 `gh workflow run` 觸發）**

| 條件 | 結果 |
|---|---|
| 7-A `mode=light slot=light` | ✅ dispatch 到結束 27 秒；「產生時段報告」步驟 skipped；`latest.json` 更新（slot=light）；`archive/2026-09-09/` 沒有新報告；新增的歷史點只有 NVDA／GSPC 的 2026-09-08（美股盤中），都是 `intraday`＋`provisional` |
| 7-B `full/morning` | ✅ `archive/2026-09-09/morning.json` 產生；`report-latest.json` 換成晨報；`producedBy` 記了 host／event／runId |
| 7-C `full/manual` | ✅ `report-latest.json` 仍是 7-B 那份晨報；手動報告在 `report-manual.json`；`archive/2026-09-09/manual.json` 也在 |
| 7-D 加一行 raise | ✅ 紅燈：「產生時段報告」failure、「存檔並推上來」skipped、零資料推上去。拿掉之後 `full/review` 綠燈，`review.json` 產生、`report-latest` 換成盤後、含「今晚觀察」 |
| 7-E 兩個 dispatch 相隔 10 秒 | ✅ 兩個 run（相隔 13 秒）都 success；第二個走了 `publish.py` 的重試路徑（commit 訊息「第 1 次重試」）；本機分片的 runAt 沒被動到 |
| 7-F | ✅ 見 7-3 |
| 7-G | ✅ 110 條全綠；對照組：時段改回用時間猜 → 紅、本機傳非 light 的 slot → 紅（見 7-1） |

**一個自己造成的小事故，照實記**：7-D 注入 raise 時用了 `git commit -a`，把當時還沒 commit 的 README 修改一起掃進那個 commit；
之後 `git revert` 整個 commit，README 的三處修改也被退掉了。已從那個 commit 把 README 撈回來（`git checkout e14930a -- README.md`），
`report.py` 確認沒有 raise。分支歷史裡因此有一對「注入／反轉」的 commit，內容含 README 的來回，無害。

**還沒做（要等你）**：7-H（cron-job.org 建好後觀察 2 小時）、7-I（隔天四份報告準時到）。（→ 已於 2026-09-18 完成，四天全部 PASS，見停點 8 的 8-1）

**怎麼退回**

```bash
git revert --no-edit stop7-3..stop7-4   # 只反轉 7-4（文件）
git revert -m 1 56f96f6                 # 整個停點 7 從 main 退掉（見 README 維運區的回滾表）
```

---

## 2026-09-09 · 停點 7 收尾 A（cron-job.org 建好後的驗證）

**cron-job.org 六個 job 各按一次 Execute now（21:10～21:17）**

| created（台北） | event | mode | slot | 結果 | 耗時 | publish 重試 |
|---|---|---|---|---|---|---|
| 21:10:48 | workflow_dispatch | light | light | success | 22s | 無 |
| 21:12:58 | workflow_dispatch | full | morning | success | 40s | 無 |
| 21:14:13 | workflow_dispatch | full | midmorning | success | 32s | 無 |
| 21:15:46 | workflow_dispatch | full | close | success | 31s | 無 |
| 21:16:53 | workflow_dispatch | full | review | success | 28s | 無 |
| 21:17:40 | workflow_dispatch | light | light | success | 23s | 無 |

六筆 `workflow_dispatch`、mode/slot 與六個 job 一一對應、全部綠燈。**零重試**：每次相隔 1～2 分鐘、單次不到 40 秒，
根本沒有撞上，所以這六次**不構成競態壓力測試**（真正的競態證據是 7-E 那兩個相隔 13 秒的 dispatch，第二個走了重試路徑）。
另有四筆 `schedule`（備援 cron，light）：05:23、07:39、13:15、19:50。備援 cron 是 `41 */3 * * *`（台北 02:41、05:41 … 23:41，一天 8 次），停點 7 在 00:32 併進 main，整天都該照這張表跑；實際這四次若各自對應最近的前一個排定時刻，分別晚了 2 小時 42 分、1 小時 58 分、1 小時 34 分、2 小時 09 分，8 個時刻裡至少 4 個完全沒跑。這正是停點 6 在量的 GitHub 排程漂移、也是要外部觸發的原因——留字據，不當問題處理，停點 6 量完（09-10）再看。

資料檢查：`latest.json` 13 項、summary ok 13、`sources.local` 沒被清空；`data/history` 對照 `stop7-4`：
沒有任何日期消失、沒有任何等級被降回去。BTC 與 WTI 的 2026-09-08 有同等級（yahoo）的收盤值微調
（78302.53→78438.58、93.64→93.03），是 Yahoo 自己修正完成 bar，不是還原。

**測試的副作用，照實記、不刪檔**：`data/archive/2026-09-09/` 裡的 `morning / midmorning / close / review`
（產生時間 21:12～21:17）是 cron-job.org 設定驗證時的**手動觸發**，不是排程產出；同一天凌晨還有 7-B／7-D 測試產的晨報（00:22）與盤後（00:28），已被晚上這兩份覆蓋；午前與收盤快報是今晚才第一次有；另有一份 manual（00:23，7-B 測試）。它們都老實記著 `producedBy.runId`，可以追。
歷史頁**沒有**另外標「手動觸發」：`producedBy.event` 對 cron-job.org 與人手按 Execute now 都是 `workflow_dispatch`，
GitHub 那一層分不出來；用既有欄位做不到，要標的話得在 dispatch 的 inputs 多帶一個來源欄位（之後再說）。
歷史頁的時段分頁本來就顯示實際產生時間（例如「晨報 21:12」），看得出不是排定時間。

**測試堵洞**：`test_slots.py` 兩條會把 `fetch_data.py` 當子程序跑的測試多帶 `--only __no_such_asset__`，
突變對照時就算把檢查拿掉也不會真的連網、不會寫真實分片（7-1 那個「無法解釋的 morning」就是這樣來的）。
`test_reports.py` 的 CLI 測試改成看原始碼（`choices=list(REPORT_SLOTS) + ["manual"]`），不再把 `report.py` 當子程序跑——
突變之下那會把真實的 archive 寫壞。
對照組（沙盒＝`git worktree`，突變＝`--slot` 改回選填且預設 morning、拿掉 local 的檢查）：
第一次做錯了——沙盒從**已提交的 HEAD** 開出來，沒帶到還沒 commit 的測試修改，結果突變之下又真的連了網
（8 次請求，寫了沙盒裡的 cloud.json／local.json 與 13 個歷史檔；只在沙盒、已整個丟棄，主倉庫 `data/` 零變動）——
這恰好又重演了一次 7-1 的事故，證明沒有這道 `--only` 時後果就是這樣。第二次把工作區的測試檔複製進沙盒再跑：
兩條都紅、失敗訊息是「--only 指定的 __no_such_asset__ 不屬於 --source …」、4.7 秒跑完、沙盒 `data/` 一個檔都沒動。
教訓：對照組沙盒一定要用工作區的檔案，不能只 checkout HEAD。

**新增 `scripts/verify_schedule.py`**（只讀不寫）：給一個日期，拉那天的 run，對照 `data/schedule.json` 算
7-H（每個排定時刻有沒有對應的 dispatch、晚幾秒，門檻 60 秒）與 7-I（四份報告是否存在、是否在排定＋15 分內產生），
多出來的 run（備援 cron、手動）另列。用 2026-09-09 跑過一次確認腳本沒壞（今天的結果不算數，排程明天才生效）。
今天跑出來 7-H 24 個時刻全部「沒有對應的 dispatch」、7-I 四份全部晚 5～11 小時、另列 19 筆——與事實相符
（cron-job.org 今晚才建、四份報告是 21 點多手動觸發的）。對照組：`--grace-minutes 700` 之後午前／收盤／盤後翻成 PASS、
晨報仍 FAIL（晚 3 分），門檻邏輯有在分辨，不是永遠 FAIL。

**還沒做（明天）**：7-H、7-I 用 `python scripts/verify_schedule.py --date 2026-09-10` 彙整。（→ 已於 2026-09-18 完成，見停點 8 的 8-1）

---

# CHANGELOG — 停點 8：台銀 5 項脫離筆電（匯率先搬上雲，黃金留在筆電）

格式同停點 7：每個子階段一段，寫「改了什麼、怎麼驗證的、怎麼退回」。標籤與子階段的對應：

| 標籤 | 子階段 |
|---|---|
| `stop8-0` | 匯率改由雲端經 FinMind 抓（含前置：`.gitignore`、probe cron） |
| `stop8-1` | 停點 6 彙整（probe 結果表）＋ 7-H／7-I 正式紀錄 |
| `stop8-2` | 黃金 3 項的去向（只有紀錄，沒有程式變動） |
| `stop8-3` | 本機腳本：互斥鎖＋「工作區乾淨」只看已追蹤的檔案 |
| `stop8-4` | 卡片自己說「這是舊資料」 |
| `stop8-5` | `schedule.json` 對齊現實 |

退回任何一段：

```bash
git revert --no-edit stop8-(n-1)..stop8-n    # 只反轉第 n 段（第 0 段用 git revert --no-edit <stop8-0 的前一個 commit>..stop8-0）
git push
```

## 背景：2026-09-10～09-18 本機那一邊發生了什麼（照 `scripts/update_local.log` 寫）

雲端這一邊（台股 4、海外 4、四份報告）這段期間全部正常，不動。出事的全在家用電腦：

| 日期 | 本機啟動 | 發生什麼 |
|---|---|---|
| 09-10（四） | 0 次 | 筆電整天沒醒 |
| 09-11（五） | 3 次，14:28:21／:45／:48 | 筆電下午才醒，Windows 把錯過的三個工作一口氣補跑，三個實例互撞（見下） |
| 09-12（六） | 0 次 | 筆電整天沒醒 |
| 09-13（日） | 兩批各 3 個，23:15 與 23:20 | 同樣的互撞；23:21 **最後一次成功** |
| 09-14～09-18 | 每天 2～4 次 | **每一次都是結束碼 4**：原因不是睡覺，是 `Claude outputs/` 這個資料夾（見下） |

**兩個不同的根本原因，不要混為一談：**

1. **09-10、09-12、09-13 白天：筆電在睡。** 現代待機（S0）機種 `WakeToRun` 無效，README 早就寫過。這是「台銀 5 項不能靠筆電」的原因。
2. **09-14～09-18：一個不相干的資料夾卡住了整條管線。** 09-13 23:22（最後一次成功的七分鐘後），Claude 桌面 App 把它產出的
   交付文件存進倉庫根目錄的 `Claude outputs/`。`update_local.ps1` 用 `git status --porcelain` 判斷「工作區乾不乾淨」，
   未追蹤的檔案也算髒 → 跳過快轉 → 發現落後 origin/main → 結束碼 4。log 原文（09-15 13:05）：

   ```
   2026-09-15 13:05:09    工作區有未提交的變動，跳過自動快轉（不動你正在改的東西）
   2026-09-15 13:05:10  本機落後 origin/main 43 個 commit 且無法快轉 —— 中止。
   ```

   落後數一路從 29（09-14 22:23）漲到 141（09-18 22:08）。09-15 筆電在 00:42、13:02、13:05、22:08 都有醒、都有跑，
   所以當時「筆電整天沒跑」的判斷是**誤判**——它跑了，只是每次都被擋。資料夾 09-18 22:1x 移到倉庫外之後才恢復。
   另外 09-16 17:35 三個完整更新工作啟動後被系統直接終止（工作排程器結束碼 `0x8007042B`），log 一行都沒留。

**互撞（8-3 要修的那個 bug）比原本以為的早、也嚴重：**

- 不是 09-11 才有。log 裡最早的 `cannot lock ref` 在 **09-01 17:52**，最早的 `index.lock` 在 **09-04 19:50:46**。
- 09-11 14:28:56 `git merge: error: Unable to create '.git/index.lock': File exists.`——撞在 ps1 的 `git merge --ff-only`，
  而 ps1 不檢查那一步的結束碼；是另一個實例剛好先把共用的 HEAD 快轉好才沒事。
  `publish.py` 的重試救的是 14:30:24 那次 `! [remote rejected] HEAD -> main (cannot lock ref …)`（「第 1 次重試」後推送成功），
  **不是** index.lock——publish 在 add／commit 撞到 index.lock 會直接結束碼 1，沒有重試。
- 09-13 23:20 那一批把一個 **0 bytes 的 `data/sources/local.json`** 推進了 commit `809b441`，下一個 commit `8b56626` 才還原。
- 用「開始」的行數算啟動次數會**低估**：多個實例同時 `Add-Content` 會掉行。09-14 22:23:21 四個工作同一秒啟動，log 只留下一行「開始」。
- 四個 Windows 工作的實際設定（`Get-ScheduledTask` 查的）：UpdateLight＝週一～五 09:00 起每 30 分、持續 8 小時；
  Morning／Midday／Close＝每天 10:05／13:05／15:05；四個都是 `StartWhenAvailable=True`、`MultipleInstances=IgnoreNew`
  （只擋「同一個工作」的第二個實例，四個不同的工作互不相擋）——筆電一醒，錯過的工作同一秒一起補跑，這就是根因。

整份 log（274KB）在動工前另外存證了一份；它超過 300KB 會自己砍成最後 500 行，09-11 的原文屆時會消失。

---

## 2026-09-18 · 前置：`Claude outputs/` 與 probe 的 cron

**改了什麼**

| 檔案 | 內容 |
|---|---|
| （倉庫外） | `Claude outputs/` 移到 `D:\Claude_use\Claude outputs\`（使用者自己移的） |
| `.gitignore` | 加一行 `Claude outputs/`：就算它再出現，`git status` 也看不到它，不會再擋住本機排程 |
| `README.md` | 維運區記下這個資料夾的來歷與事故 |
| `.github/workflows/probe-bot.yml` | 拿掉 `schedule`（兩行）。量測 09-10 就到期了，但 cron 之後每 3 小時還是起一個什麼都不做的 run，白白累積了 28 個；`workflow_dispatch` 與到期檢查原樣留著 |

根治（未追蹤的檔案不該算「工作區不乾淨」）放在 8-3 跟互斥鎖一起做。

**怎麼驗證的**：`git check-ignore -v "Claude outputs/x.md"` 命中 `.gitignore:23`；probe-bot.yml 的 `on:` 只剩 `workflow_dispatch`。

**怎麼退回**：跟 8-0 在同一個 commit，見下一段。

---

## 2026-09-18 · 8-0 匯率改由雲端經 FinMind 抓

**為什麼**：台銀擋雲端，匯率只能靠筆電；筆電一睡（或像上面那樣被卡住）匯率就停——09-11 之後的匯率歷史一片空白。
FinMind 的 `TaiwanExchangeRate` 轉載的就是台銀的每日牌價，免 token，雲端抓得到。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/fetch_data.py` | 新 type `finmind_fx`：`parse_finmind_fx`（解析＋三條誠實規則）、`fetch_finmind_fx`（一個幣別一個請求）、`handle_finmind_fx`；`DATE_SOURCE` 與 `DATE_SOURCE_GRADE` 登記 `finmind`（第 3 級，跟 `csv` 同級）。`bot_fx` 的程式與測試原樣留著當「直接抓台銀」的備援路徑 |
| `data/assets.json` | `fx_usd`、`fx_cny`：`owner` local→**cloud**、`type` bot_fx→**finmind_fx**、加 `cadence: full`（一天一筆，盤中每 30 分去問沒有意義） |
| `scripts/merge_latest.py`、`scripts/cleanup_fake_history_points.py` | `BOT_TYPES` 加 `finmind_fx`（每日牌價是定案值、週末不掛牌，兩條規則照樣適用） |
| `scripts/report.py` | 報告裡的匯率多兩個小標籤：「資料來源 台銀每日匯率（經 FinMind）」「資料日期」 |
| `js/app.js`、`index.html` | 卡片小字列多印 `sourceLabel`（文字由資料層給，呈現層不猜）；今天那筆還沒發布時的說明文字；頁尾與免責的來源說明；頁尾「更新方式」原本寫「每日三時段」，是停點 7 之前的舊話，改成現況 |
| `scripts/audit_finmind_fx.py`（新） | 只讀的稽核：FinMind vs 本站既有歷史逐日比對，超過 0.3% 結束碼 1 |
| `scripts/test_finmind_fx.py`（新） | 20 條測試，全部不連網 |
| `scripts/test_schedule_util.py` | 「只有誰寫了 cadence」的確切清單：`["gold_bar", "fx_usd", "fx_cny"]` |
| `scripts/test_race_recovery.py` | 項數不寫死（從 assets.json 算）；剛換 owner、對方分片還沒有那一項時不會 `KeyError` |
| `scripts/update_local.ps1`、`.github/workflows/update-data.yml` | **只改說明文字與 commit 訊息**（「5 項」「8 項」「含匯率」），邏輯一個字沒動 |
| `README.md` | 標的表、分工表（整張重寫，舊表還停在停點 7 之前）、已知限制、`dateSource` 表、資料來源 |

**設計上的三個決定**

1. **純追加**。每次只問「歷史最後一天」起的資料，而且只收比它**新**的日期。舊的 132 個 `csv` 點與 4 個沒有 `dateSource` 的老點
   從此不會被重送，也就不可能被改寫（同等級的新點會整筆蓋過去、連 `dateSource` 一起洗掉——所以不靠等級保護，靠「不重送」）。
   含最後一天是為了對帳：來源對那一天的說法跟本站不同時只提醒、不覆寫。代價：FinMind 事後修正某天的值我們不會跟。
2. **今天那筆還沒發布不算失敗**。`msg=success` 但沒有新日期 → `status=ok`、卡片顯示最後一筆的日期（不是今天）、
   `historyNote` 說明原因、**歷史檔完全不碰**（連 `updatedAt` 都不改，少掉每次 full 一筆無意義的 diff）。
   `msg` 不是 success、`status` 不是 200、`data` 不是陣列 → 失敗，走既有的 `status=error`＋`lastGood`，一個點都不寫。
   注意 FinMind 出錯時 **HTTP 照樣回 200**，`msg` 這個判斷是唯一的防線。
3. **輕量更新沿用上次結果，但上次沒有結果或失敗就照抓**（跟實體條塊同一個寫法）。否則換 owner 之後、第一次完整更新之前，
   兩張匯率卡會因為「雲端分片裡還沒有這一項」而一路紅著。

卡片上的價格、買賣價、日期全部出自同一列（以前現價來自沒有日期的當日 CSV、日期來自歷史最後一點，會出現「09-13 抓的價格配 09-11 的日期」）。
現金買入／賣出只放在卡片，不進歷史點，歷史點的形狀不變。

**怎麼驗證的**

- **歷史稽核（0.3% 規則）**：`python scripts/audit_finmind_fx.py`，2026-09-18 22:33，共 2 個請求。

  | 標的 | 本站歷史 | 逐日比對 | 完全一致 | 有差異 | 超過 0.3% | 只有一邊有的日期 |
  |---|---|---|---|---|---|---|
  | fx_usd | 136 點（03-02～09-11） | 136 天 | **136** | 0 | 0 | 無 |
  | fx_cny | 136 點（03-02～09-11） | 136 天 | **136** | 0 | 0 | 無 |

  即期買入與即期賣出兩個欄位都比。切換後會補進歷史的新日期：09-14、09-15、09-16、09-17、09-18 共 5 天
  （CNY 即期賣出 4.75／4.768／4.779／4.777／4.774；USD 31.75／31.9／31.94／31.93／31.855）。
- 單元測試 `python -m unittest discover -s scripts -p "test_*.py"`：**130 OK**（原 110 ＋ 新 20）。`python scripts/test_race_recovery.py`：全部通過（在「assets.json 已換 owner、雲端分片還沒有匯率」的過渡狀態下跑的）。
- **突變對照組**（每一個案例：把工作區現在的檔案整份複製到暫存目錄 → 在副本改壞一處 → 只跑 `test_finmind_fx.py` → 必須紅）。
  11 個全部符合預期：

  | 改壞的方式 | 結果 |
  |---|---|
  | （基準）什麼都不改 | 綠（20 條全過） |
  | 把 `msg` 的判斷改壞（"success" 改成別的字，永遠不會擋） | 2 條紅：`test_failure_message_writes_nothing_at_all`、`test_message_other_than_success_is_a_failure_even_with_data` |
  | 拿掉 `status` 必須是 200 的判斷 | 1 條紅 |
  | 沒有新點時也回傳 `new_pts`（main 會拿空陣列去存檔＝清空歷史） | 3 條紅 |
  | 拿掉「只收比歷史最後一天新的日期」 | 1 條紅：`test_old_days_are_never_rewritten_even_if_the_source_disagrees` |
  | 歷史點的日期改用「現在」而不是回應的 `date` | 3 條紅 |
  | 拿掉輕量更新的跳過 | 1 條紅 |
  | 每次都從 2026-03-02 整段重抓 | 1 條紅 |
  | `DATE_SOURCE_GRADE` 忘了登記 `finmind` | 1 條紅 |
  | `assets.json` 的 fx_usd 忘了寫 `cadence=full` | 1 條紅 |
  | `merge_latest` 的 `BOT_TYPES` 忘了加 `finmind_fx` | 1 條紅 |

  「失敗時一個點都不准寫」那兩條測試刻意走到 `main()` 的存檔那一步，而且假回應**故意帶著看起來正常的資料列**：
  handler 自己從來不寫檔，只驗 handler 的話，把 `msg` 判斷拿掉之後歷史檔也「看起來」沒被動過，對照組會是假的綠。
- **端到端（暫存副本，真的連 FinMind，2 個請求）**：`fetch_data.py --source cloud --slot manual --only fx_usd,fx_cny` →
  兩項各 141 點、最新一筆 09-18、雲端分片 10 項；`merge_latest.py` → 13 項 ok、fx `source=cloud`、`freshness=fresh`。
  歷史檔的 diff 只有檔頭的 `count`／`updatedAt` 與**新增的 5 行**，舊的 136 行一個字元都沒變。
- **本機預覽**（`python -m http.server` 開暫存副本）：匯率卡小字列「即期賣出 · 台幣 / 1 人民幣 · 2026-09-18 · 台銀每日匯率（經 FinMind）」、
  四個買賣價都在、console 無錯誤；把副本裡 fx_cny 的日期改回前一天 → 出現「（最近一筆）」與說明框
  「這是台銀的每日牌價，經 FinMind 取得；當天那一筆要等它發布才有…」。

**FinMind 當天那一筆幾點出現**（誠實條款，持續補）：目前只有兩個下界——09-16 的那一筆在 09-16 22:16 已經存在；
09-18 的那一筆在 09-18 22:33 已經存在。09:30 與 15:30 兩次完整更新各觀察一次的結果，等 8-0 上線後的第一個營業日
（09-21）從 Actions 的 log 讀（handler 每次都會印「FinMind USD 最新一筆：…（就是今天／不是今天）」），再補進這裡與 PLAN 7.11。

**怎麼退回**

```bash
git revert --no-edit stop8-0          # 前置與 8-0 是同一個 commit；assets.json 的 owner 會回到 local
git push
```

退回之後匯率又歸家用電腦：雲端分片裡殘留的那兩項會被 merge 當成孤兒略過（無害），本機下一次完整更新就會接手。
FinMind 補進去的歷史點（`dateSource=finmind`）不會被退回，它們是真的牌價，留著沒有問題。

---

## 2026-09-18 · 8-0 上線後的實跑驗收（22:46～22:50）

8-0 單獨先合回 main（merge commit `6ce8498`），因為它的驗收只能在正式環境發生。

| 驗收 | 結果 |
|---|---|
| 手動觸發雲端 `mode=full slot=manual`（run 35358347438） | ✅ success。log：`FinMind USD 最新一筆：2026-09-18（就是今天）`、CNY 同；`已寫出分片 data/sources/cloud.json（10 項）`；成功 10／失敗 0，共 11 次請求（其中 FinMind 2 次）。**從 GitHub 的機器抓得到 FinMind。** |
| 雲端提交了匯率的歷史檔 | ✅ commit `52c9f9e`（github-actions[bot]）含 `data/history/fx_usd.json`、`fx_cny.json`；diff 只有檔頭的 `count`／`updatedAt` 與新增的 5 行（09-14～09-18，`dateSource=finmind`），舊的 136 行一字未動 |
| `latest.json` | ✅ fx_usd／fx_cny：`source=cloud`、`freshness=fresh`、`date=2026-09-18`、`points=141`、`sourceLabel=台銀每日匯率（經 FinMind）`；下一個排定點 09-21 09:30（cadence=full、週末不排） |
| 本機實跑一次 `update_local.ps1`（完整更新，22:47:59） | ✅ 結束碼 0，09-13 以來第一次成功。log：`標的數：3`（gold_twd、gold_cny、gold_bar）、`已寫出分片 data/sources/local.json（3 項）`、對台銀 5 次請求、`推送成功`。commit `5175557`「本機補抓（台銀黃金）」只動了三個黃金歷史檔、`local.json`、`latest.json`——**沒有碰匯率的任何檔案** |
| 合併結果 | ✅ 13 項全部 ok、全部 fresh；`sources.cloud` ok=10、`sources.local` ok=3 |
| 線上網站（`https://davidjjx.github.io/invest-watch/`，22:49） | ✅ 美元／台幣 31.855、人民幣／台幣 **4.774**，小字列「即期賣出 · … · 2026-09-18 · 台銀每日匯率（經 FinMind）」，四個買賣價都在；頁尾已是新文字；`app.js?v=20260918-1` |

規格寫的驗收值是「09-15 的 4.768」；實際上線時 FinMind 已經有 09-18 那一筆，所以卡片顯示的是 09-18 的 4.774，
09-15 的 4.768 在歷史檔裡（與稽核表一致）。

---

## 2026-09-18 · 8-1 停點 6 彙整 ＋ 7-H／7-I 正式紀錄

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `README.md` | 設計筆記新增「停點 6 的量測結果」：12 個 run × 5 個網址的結果表、量之前就寫死的判定規則、結論與這個結果的界線；進度區把「7-H／7-I 要等 09-10」收掉 |
| `.github/workflows/probe-bot.yml` | cron 已在前置那個 commit 拿掉（使用者要求立刻做），這裡不再動 |
| `docs/CHANGELOG.md` | 這一段；停點 7 那兩句「還沒做 7-H／7-I」補上去向 |

不改任何程式。

**probe 彙整**（`gh run list`＋逐一 `gh run view --log` 抓 `PROBE_RESULT`，只讀；不碰台銀）

- probe-bot 共 48 個 run，全部是 `schedule`：**有量測結果的 12 個**（09-08 12:51～09-10 19:31），
  其餘 36 個（09-11 00:32～09-18 19:28）是到期後的空跑，log 只有「量測期間已於 2026-09-10 結束」。
- 60 個樣本：**驗證頁 60、通過 0、連線失敗 0、HTTP 錯誤 0**；順位 1～5 都出現過；12 台不同的 runner。
- 三組判定（每組全部樣本通過才算）：匯率組 0/24、存摺組 0/24、條塊組 0/12 → **三組全部不通過**。
- 完整的表在 README 設計筆記。

**7-H／7-I**（`python scripts/verify_schedule.py --date …`，只讀）

| 日期 | 7-H：24 個排定時刻都有對應的 dispatch | dispatch 晚幾秒（最小／最大／平均） | 7-I：四份報告產生時間 |
|---|---|---|---|
| 09-10（四） | PASS 24/24 | 12／25／18.0 | PASS：09:30:56、11:30:49、13:35:43、15:30:58 |
| 09-11（五） | PASS 24/24 | 12／25／18.1 | PASS：09:30:52、11:31:01、13:35:42、15:30:53 |
| 09-14（一） | PASS 24/24 | 11／25／17.2 | PASS：09:30:59、11:30:48、13:35:37、15:30:52 |
| 09-15（二） | PASS 24/24 | 12／23／17.3 | PASS：09:30:54、11:30:49、13:35:38、15:30:49 |

門檻是 dispatch 晚 60 秒內、報告在排定＋15 分內。四天 96 個排定時刻沒有一個漏掉，最晚 25 秒；
十六份報告都在排定後 61 秒內產生。每天另有 4～5 筆不對應排定時刻的 run（備援 cron 的 light），另列、不計分。
規格要的是 09-14 與 09-15；09-10 與 09-11 是 cron-job.org 上線後的頭兩天，README 原本寫著要等它們，一併補驗。

**怎麼驗證的**：彙整腳本對自己的結論做了交叉檢查——樣本數＝run 數×5、三組樣本數相加＝60、
分類用 `fingerprint.title`（`Challenge Validation`）與錯誤訊息兩個獨立欄位，兩者結論一致。
對照組：把判定從「全部通過才算」改成「有一個通過就算」→ 結論仍是不通過（因為通過數是 0），
所以這張表對判定規則的寬嚴不敏感，結論不是規則選出來的。

**怎麼退回**

```bash
git revert --no-edit stop8-0..stop8-1   # 只有文件
```

---

## 2026-09-18 · 8-2 黃金 3 項的去向（只有紀錄）

**決定**：8-1 的存摺組與條塊組都不通過 → 黃金存摺 TWD／CNY、實體條塊**留在家用電腦**。使用者 2026-09-18 拍板：
這一輪把筆電這條路修好（8-3），下一階段 8-B 另案做「雲端用國際金價換算的估算序列當保底，台銀官方價留筆電並誠實標示」。
本停點**不做**估算序列。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `README.md` | 設計筆記新增「黃金 3 項的去向」：決定、下一階段的方向、以及日後若要把直接抓台銀搬上雲，規格必須先補的四件事 |
| `data/assets.json`、`data/schedule.json`、`scripts/fetch_data.py`、`.github/workflows/update-data.yml` | **不動**。黃金 3 項 `owner=local`；規格裡「通過的組」那一整段（改 owner、連續 3 次被擋退避、雲端實測、本機無標的時結束碼 0、停用 Windows 工作）都沒有觸發 |

**怎麼驗證的**：`git diff stop8-1..stop8-2 --stat -- data scripts .github js css *.html` 是空的；
`data/assets.json` 的 owner 統計仍是 cloud 10／local 3（黃金 3 項）。

**怎麼退回**

```bash
git revert --no-edit stop8-1..stop8-2   # 只有文件
```

---

## 2026-09-18 · 8-3 本機腳本：互斥鎖 ＋「工作區乾淨」只看已追蹤的檔案

黃金 3 項留在筆電，所以這一段要做。目標：筆電這條路「醒著就一定會成功」。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/update_local.ps1` | ① **互斥鎖**：具名 Mutex（`Global\InvestWatch-update_local-<倉庫路徑雜湊>`），拿不到就寫一行「另一個實例執行中，略過」、結束碼 **0**。接住 `AbandonedMutexException`（上一個持有者沒放鎖就死了＝鎖已經是我們的）。拿到鎖之後的每個出口都走 `Exit-Script` 先放鎖。② **「工作區乾淨」只看已追蹤的檔案**：`git status --porcelain --untracked-files=no`。③ **死掉的 `index.lock`**：拿到互斥鎖＋當下沒有任何 git 程序＋檔案放超過 2 分鐘，三個都成立才清並記 log；否則記 log 不動它。④ **`Write-Log` 改成獨占開檔再附加**（見下）。log 自砍挪到鎖後面 |
| `scripts/publish.py` | `dirty_paths()` 同樣加 `--untracked-files=no`，跟 ps1 的判斷一致 |
| `scripts/test_update_local_lock.py`（新） | 沙盒實測，正式組＋對照組（`--contrast`） |
| `scripts/test_publish.py` | 加一條：`dirty_paths` 一定帶 `--untracked-files=no` |
| `README.md` | 結束碼表加「0／另一個實例執行中，略過」、index.lock 的兩種訊息、沙盒測試怎麼跑；`Claude outputs/` 事故補上根治方式 |

**為什麼用 Mutex 不用 lock 檔**：持有鎖的程序不管怎麼死（工作排程器的時間上限、電腦睡著時被終止），
Windows 都會自己把鎖收回，不會留下一個要人手動刪的死檔——lock 檔正好會製造出跟 `index.lock` 一樣的問題。
鎖名帶倉庫路徑的雜湊，所以沙盒測試跟正式排程互不干擾；`Global\` 讓工作排程器啟動的與手動在視窗跑的互相看得到。

**做到一半才發現的事：log 掉行不是「寫不進去」，是「互相蓋掉」。** 第一版只給 `Add-Content` 加了重試，
沙盒實測照樣掉行（第 2 輪「開始」0 次、「略過」只有 1 次）。原因：多個程序同時 `Add-Content` 不會報錯，
而是各自從當時的檔尾寫下去，後寫的蓋掉先寫的。改成 `[System.IO.File]::Open(…, Append, Write, FileShare.None)`
獨占開檔、開不了就稍等重試（最多 40 次），之後三輪都是「開始 1 次、略過 3 次」，一行不掉。
這也解釋了背景段說的「用『開始』的行數算啟動次數會低估」。

**怎麼驗證的**

- 驗收與對照組**都不在正式倉庫做**：同時啟動多個實例等於對台銀打數倍請求、在公開倉庫製造互撞 commit，
  而且 ps1 在非 main 分支根本跑不到抓取。`python scripts/test_update_local_lock.py` 在暫存目錄建假遠端（bare repo）
  與假筆電，種子用**工作區現在的檔案**，把 `fetch_data.py` 換成替身（睡 4 秒、改寫本機分片的時間戳、不連網），
  `merge_latest.py` 與 `publish.py` 用真的，真的啟動 PowerShell 跑 ps1。
- **正式組：33 項檢查全部 PASS**

  | 情境 | 結果 |
  |---|---|
  | A. 同時啟動 **4** 個 ps1（2026-09-14 22:23 真的發生過四個工作同一秒啟動），連做 3 輪 | 每一輪：4 個結束碼都是 0、「開始」1 次、「另一個實例執行中，略過」3 次、log 沒有任何 `index.lock`／`cannot lock ref`／`rejected`、遠端**剛好**多 1 個 commit、本機分片是完整的 JSON、工作區乾淨 |
  | B. 倉庫根目錄放一個陌生的空資料夾、一個裝著文件的陌生資料夾、一個陌生檔案，而且遠端比本機新一個 commit | 結束碼 0；log 沒有「跳過自動快轉」、有 `git merge: … Fast-forward`；抓取與推送都做了；HEAD＝遠端 main；陌生檔案沒有被 commit、原封不動還在 |
  | C. `.git/index.lock` 放了 10 分鐘、沒有 git 在跑 | log「已清掉」、鎖檔消失、這一輪照常完成 |
  | C. 剛建立的 `index.lock` | **沒有**被刪、log「不動它」（那一輪結束碼 1，符合預期：你可能正在用 git） |

- **對照組（`--contrast`，把沙盒裡 ps1 的修正拿掉）：問題全部重現**

  | 拿掉的修正 | 結果 |
  |---|---|
  | 互斥鎖（每個實例都當自己拿到了鎖） | **第 1 輪就重現**：`fatal: Unable to create '…/.git/index.lock': File exists.`、`Another git process seems to be running in this repository`；4 個實例有 2 個結束碼 1、「開始」4 次 |
  | `--untracked-files=no` | **重現結束碼 4**；log「跳過自動快轉」「無法快轉 —— 中止」；什麼都沒推上去——就是 09-14～09-18 那五天的樣子 |
  | 死鎖檔清理 | 鎖檔留著、這一輪結束碼 1、沒有任何 commit；log 看得到 `index.lock` 的錯誤 |
  | `publish.py` 的 `--untracked-files=no` | `test_dirty_paths_ignores_untracked_files` 紅 |

- 單元測試 131 OK；`test_race_recovery.py` 全部通過（publish.py 有改所以重跑）；ps1 語法檢查 0 錯誤、UTF-8 BOM 還在（PowerShell 5.1 沒有 BOM 會把中文當 Big5）。

**已知取捨**

- 筆電一醒同時補跑的三、四個工作只剩一個真的跑。它們做的是同一件事（本機一律 `--slot light`；不帶 `-Light` 就是完整更新），
  但如果活下來的剛好是 UpdateLight，那一次就只有輕量更新——下一個排定時間會補上。
- 根因（四個工作各自「錯過就補跑」、互不相擋）沒有動，那要改 Windows 工作排程器的設定，不在這一輪的範圍。
- 開發期間（倉庫停在功能分支上）本機排程會以結束碼 3 中止，這是既有的設計；今晚沒有排定的本機工作，沒有影響。

**怎麼退回**

```bash
git revert --no-edit stop8-2..stop8-3
```

---

## 2026-09-18 · 8-4 卡片要自己說「這是舊資料」

**為什麼**：09-10、09-13、09-14～18 三次，網站上的黃金與匯率都停在好幾天前，資料裡老實標了 `stale`，
但卡片上就是那個數字、沒有任何提示——三次都是使用者自己比對數字才發現。前端從來沒有讀過 `freshness` 這個欄位。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `js/freshness.js`（新） | 純函式 `Freshness.classify(標的, 現在)` → 要掛什麼標示（黃／紅／灰）、要不要隱藏價格、carriedOver 的小字、要不要收掉「今天還沒有新報價」那段黃字。不碰 DOM、不發請求 |
| `js/app.js` | 卡片去問 `freshness.js`；原本叫 `stale` 的變數其實是「牌價日期不是這次更新的那一天」，改名 `dateBehind` 免得兩種意思混在一起；carriedOver 統一成一行灰色小字；`freshness=error` 時價格與買賣價都不顯示；網址帶 `?now=` 可以假裝「現在」（頁面上方會明講是示範模式） |
| `css/style.css` | 三個最小樣式 `.fresh-badge.stale／error／weekend`，沿用既有的顏色變數，不動版面 |
| `index.html` | 在 `app.js` 之前載入 `freshness.js`；`bump_assets.py` 更新版本號 |
| `scripts/test_freshness.html`、`scripts/test_freshness_js.py`（新） | 16 題寫死的假資料＋假時間，用無頭 Edge／Chrome 開頁面、把 DOM 倒出來逐題檢查；另外 2 條檢查卡片真的有接上 |
| `README.md` | 「資料壞掉的時候會怎樣」新增一節，列出五種情況各看到什麼、規則在哪、已知限制 |

**跟規格字面不同的兩個地方（理由）**

1. **週末灰色看的是牌價日期，不是「最後成功時間是週五」。** `lastSuccessAt` 是機器去抓的時間：本機排程週六日也跑，
   筆電週日 23:21 抓成功時它是週日，牌價卻是週五 19:57。若筆電週六醒來抓過一次再睡到週日，照字面會顯示黃色
   「沿用週六 14:00 的資料」——但那筆就是週五牌價、也就是現行牌價，該灰不該黃。改看 `a.date`（牌價自己的日期）
   是不是剛過去的星期五，兩種情況都對；規格裡的驗收（週六＋週五的資料 → 灰）照樣成立。
   台股 8 項雲端週末照抓，`lastSuccessAt` 永遠是週末當天，照字面永遠進不了灰色。
   週末時就算 `freshness=fresh` 也掛灰色、並收掉原本那段黃色的「今天還沒有新報價」——那正是「週五的數字被說成舊資料」的來源。
2. **「把 lastSuccessAt 改成 3 小時前 → 黃色」不一定成立，這是對的。** 新舊比的是「上一個排定的更新時間」不是「距今多久」：
   週五 23:45 把黃金存摺的成功時間改成 3 小時前（20:45），它還是 fresh——因為本機最後一個排定點是 17:00。
   示範改用「早於最後一個排定點減 30 分寬限」的時間（當天 15:00）。前端不自己用距今多久算，那會重演停點 2 砍掉的誤判。

**怎麼驗證的**

- 單元測試：`test_freshness_js.py` 19 條全綠（17 條經無頭瀏覽器、2 條看原始碼接線）；全套見下一段。
- **驗收示範（沙盒副本＋`python -m http.server`，真資料、不 commit）**：
  1. 把沙盒裡本機分片的 gold_twd `lastSuccessAt` 改成當天 15:00、gold_cny 改成沒有成功紀錄 → 重跑 `merge_latest.py`
     （新舊判定：error 1、fresh 11、stale 1）→ gold_twd 卡片出現**黃色**「沿用 09-18 15:00 的資料（牌價日期 2026-09-18）——排定的更新沒有跑到」；
     gold_cny **紅色**、價格變「—」、買賣價收起來；其餘 11 張沒有任何標示。
  2. 分片改回原樣、重跑合併（fresh 13）→ 頁面上 `.fresh-badge` 0 個，價格回來。**黃色出現、改回就消失。**
  3. 網址加 `?now=2026-09-19T10:00:00+08:00`（週六）→ 台銀黃金 3 張與台股 4 張全部**中性灰**
     「週末不掛牌／不開盤，沿用週五 …」，海外 4 張沒有；頁首出現紅色「示範模式」。**沒有改系統時間**
     （那會讓四個 Windows 工作大量補跑、也會弄髒資料裡的時間戳）。
  4. console 零錯誤；兩張示範截圖存檔（檔名標明 SANDBOX，不是線上）。
- **突變對照組**（工作區整份複製到暫存目錄 → 改壞一處 → 只跑 `test_freshness_js.py` → 必須紅），9 個全部符合預期：

  | 改壞的方式 | 結果 |
  |---|---|
  | （基準）什麼都不改 | 綠（19 條全過） |
  | 把 stale 的判斷改壞（`'stale'` 改成 `'fresh'`） | 8 條紅（含 `test_stale_is_yellow_and_says_since_when`、`test_fresh_has_no_badge`） |
  | 週末的灰色不再要求「牌價日期是星期五」 | 1 條紅：test_weekend_but_the_quote_is_older_than_friday_is_yellow |
  | 星期幾改用瀏覽器的時區算（拿掉 +8 小時） | 1 條紅：test_weekday_is_judged_in_taipei_not_in_the_browsers_timezone |
  | freshness=error 時照樣顯示價格 | 1 條紅：test_freshness_error_is_red_and_hides_the_price |
  | 週末那個理由也套用到海外標的 | 1 條紅：test_overseas_assets_never_get_the_weekend_excuse |
  | carriedOver 不再產生小字 | 2 條紅：test_carried_over_is_small_text_with_the_reason、test_carried_over_without_a_reason_says_when_it_was_fetched |
  | app.js 不去問 freshness.js（函式寫對了但卡片沒接上） | 1 條紅：test_app_js_asks_freshness_js_and_does_not_judge_by_itself |
  | 找不到瀏覽器（IW_BROWSER 指到不存在的路徑）→ 必須紅，不是跳過 | 1 條紅：setUpClass |

  最後一列是刻意的：找不到瀏覽器時這一組是**紅**的，不是跳過——「測試沒跑」不可以看起來像「測試過了」。

**怎麼退回**

```bash
git revert --no-edit stop8-3..stop8-4
```

---

## 2026-09-18 · 8-5 `schedule.json` 對齊現實

本機仍有標的（黃金 3 項），所以 `local` 那一段要留著、而且要描述現實。

**先查現實，再動檔案。** 規格的前提是「log 證明 UpdateLight 整天每 30 分鐘跑，不是 09:00–17:00」。
用 `Get-ScheduledTask` 查四個工作的實際設定（09-14、09-18 各查一次，結果相同）：

| 工作 | 觸發 | 重複 | 錯過就補跑 | 同一工作重複啟動 |
|---|---|---|---|---|
| InvestWatch-UpdateLight | 每週，`DaysOfWeek=62`（一～五），09:00 | 每 `PT30M`、持續 `PT8H`、到期即停 | True | IgnoreNew |
| InvestWatch-Morning／Midday／Close | 每天 10:05／13:05／15:05 | 無 | True | IgnoreNew |

**設定值跟 `schedule.json` 原本寫的一字不差**（`light` 平日 09:00～17:00 每 30 分、`full` 每天三個時間）。
log 裡排程時間以外的執行全部是「錯過就補跑」造成的，而且行為不固定：

| 日期 | 本機實際啟動的時間（log 的「開始」） | 說明 |
|---|---|---|
| 09-07 | 16:12、17:28、20:36、20:42、21:12 … 23:42、隔天 00:12 | 20:42 起每 30 分連跑到 00:12（都是 `--light`） |
| 09-09 | 19:50 ×2、20:20、20:50 … 23:20 | 19:50 補跑後每 30 分連跑到 23:20 |
| 09-16 | 21:26、21:35、22:05、22:35 | 補跑後每 30 分 |
| 09-18 | 18:36、19:06、22:07 | 22:07 之後筆電一直醒著，**22:37、23:07 都沒有再跑** |

「補跑之後 8 小時的重複窗從補跑那一刻重算」可以解釋 09-07（16:12＋8 小時＝00:12），卻解釋不了 09-18。
所以它只能算觀察，不是規則；什麼時候補跑取決於筆電什麼時候醒，寫不成 `days／from／to`。

**決定**：`local.full`／`local.light` 的值**不動**——它們已經是現實（設定的排程）。
把 `light` 改成整天反而不對：`freshness` 會在晚上與半夜（台銀根本沒有新牌價）一直判成過期，那是誤判，
而且 8-4 之後卡片會因此整晚掛著黃色。改的是「把查到的現實寫下來」。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/test_schedule_util.py` | 補一條對真檔案的健檢 `test_every_days_field_only_names_real_weekdays`（對照組抓到的漏洞，見下） |
| `data/schedule.json` | `local` 加 `_排程在哪裡`（跟 `cloud` 那一段同樣的寫法）：四個工作的實際設定、查證日期、本機現在只負責黃金 3 項、補跑的觀察（含不一致的那一天）、為什麼不把 light 寫成整天。`full`／`light` 的值一個字沒動 |
| `README.md` | 四個 Windows 工作的表照現實重寫（舊表還寫著本機會產晨報／午盤／收盤報告，那是停點 7 之前的事）；加上查實際設定的一行 PowerShell 與怎麼讀它的輸出 |

**怎麼驗證的**

- `python -m unittest discover -s scripts -p "test_*.py"`：151 OK（`TestRealScheduleFile` 會重新健檢真的 `schedule.json`）；
  用新檔對真資料合併一次：13 項、新舊判定與改之前一致（值沒動，只多了說明）。
- 突變對照組（只跑 `test_schedule_util.py`）：

  | 改壞的方式 | 結果 |
  |---|---|
  | （基準）什麼都不改 | 綠 |
  | `local.light` 的 `days` 寫成 `"8"`（不存在的星期） | **第一次沒有紅**，補測試之後 1 條紅：`test_every_days_field_only_names_real_weekdays` |
  | 整個 `local` 段刪掉（本機明明還有黃金 3 項） | 紅：`test_every_owner_has_a_usable_schedule` 等 |

  第二列第一次跑是綠的——對照組失敗。原因：`parse_days` 對超出範圍的數字取 7 的餘數，`"8"` 不會報錯，
  而是被安靜地當成週一；真檔案的健檢只看「算不算得出時間」，看不出時窗被搬到別天。
  這正是對照組存在的理由：沒有它，我會以為這個檔案有測試在顧。補了一條只針對真檔案的健檢（days 只能用 0～6），
  重跑之後紅。`parse_days` 本身沒有動（改它的行為不在這一段的範圍）。
- 人工核對：`Get-ScheduledTask` 的輸出（上表）與 `schedule.json` 的 `local.full`／`local.light` 逐項一致。

**怎麼退回**

```bash
git revert --no-edit stop8-4..stop8-5
```

---

## 2026-09-18 · 停點 8 收尾：合回 main 之後的實跑（23:25～23:35）

8-1～8-5 合回 main 的 merge commit 是 `3ffe155`（8-0 是 `6ce8498`）；六個標籤 `stop8-0`～`stop8-5` 都已推上遠端。

| 實跑 | 結果 |
|---|---|
| 手動觸發一次雲端 `mode=light slot=light`（run 35362593804） | ✅ 匯率兩項 log：「跳過：台銀每日匯率一天一筆，盤中的輕量更新不重抓（沿用上次結果）」；這一輪共 5 次請求，**沒有任何一次打 FinMind**；分片仍是 10 項 |
| 【0】c 在**真倉庫**上實測：倉庫根目錄放一個陌生的空資料夾、一個裝著文件的陌生資料夾、一個陌生檔案，此時遠端比本機新 1 個 commit，跑 `update_local.ps1 -Light` | ✅ 結束碼 0。log：`git merge: Updating 3ffe155..b1b1d08`／`Fast-forward`，**沒有「跳過自動快轉」**；`標的數：3`、實體條塊跳過、對台銀 3 次請求；`推送成功`（commit `5bf7257`）。陌生檔案沒有被 commit，測完已刪，`git status` 乾淨 |
| 線上網站（23:31，`app.js?v=20260918-2`、`freshness.js` HTTP 200） | ✅ 13 項全部 fresh，沒有黃色或紅色；匯率兩張卡與實體條塊各有一行灰色小字「這一輪未更新，沿用上次結果（…）」（最近一輪是輕量更新）；匯率仍是 09-18 的 31.855／4.774 |
| 線上網站加 `?now=2026-09-19T10:00:00+08:00`（模擬週六） | ✅ 黃金 3、台股 4、匯率 2 共 9 張卡是中性灰「週末不掛牌／不開盤，沿用週五 …」，海外 4 張沒有；頁首紅色「示範模式」 |

**還沒做、要等時間的事**

- FinMind 當天那一筆幾點出現：要等第一個營業日（2026-09-21）09:30 與 15:30 兩次完整更新，
  到 Actions 的 log 找「FinMind USD 最新一筆：…（就是今天／不是今天）」，再補進 8-0 那一段與 PLAN 7.11。
- 真正的驗收是哪天筆電整天關機、匯率照樣在 09:30 之後更新——09-21 起看得出來。黃金 3 項筆電睡著時仍會停，
  卡片會掛黃色；那要等下一階段 8-B（雲端估算序列）或一台不會睡的機器。
- 資料裡另有一則跟這次無關的既有警告：`wti 有 1 個暫定點留過夜沒被定案：2026-09-13`（週日的盤中點），沒有處理，留給之後。


---

# CHANGELOG — 分析系列：A0 資料來源探測 → A1～A4 各面向事實卡

格式同停點 7、8：每個停點一段，寫「改了什麼、怎麼驗證的、怎麼退回」。這個系列的共同鐵則寫在每個停點 Prompt 的前面，摘要：
**絕不做加權總分**——每個面向獨立表態＋一句理由；每個指標標證據等級（★★★／★★／★，★ 級註明「參考性低」）；
資料不足就顯示「資料不足」、估算值標明估算；分析只在雲端 15:30 那一輪完整更新算一次、輕量模式完全不碰；
不增加對台銀的任何請求；任何需要「我的持倉」參與的計算一律在瀏覽器裡做、結果不寫回公開倉庫；
頁尾固定「以上為量化整理，未經回測驗證，不構成投資建議。」

| 標籤 | 停點 |
|---|---|
| `stopA0` | 新資料來源探測——只測不接（探測腳本＋離線測試＋手動 workflow；結果在本機 PLAN.md 7.12） |
| `stopA1` | （待排）資料層：`assetClass`、去識別化的公開分析文件、第一批指標 |
| `stopA2` | （待排） |
| `stopA3` | （待排） |
| `stopA4` | （待排） |

A0 是兩段式合併（先合併只讀的工具、跑完探測再合併文件），退回要退兩個合併：

```bash
git revert -m 1 <第二次合併的 commit> && git revert -m 1 3782bbe && git push    # 先退文件那一次，再退工具那一次；編號用 git log --oneline --merges -3 查
```

## 2026-09-21～09-23 · A0 新資料來源探測（只測不接）

**為什麼要測**：分析系列 A1～A4 需要 10 年長歷史、美債殖利率、CPI、CAPE、ETF 淨值等這個專案沒抓過的東西。
停點 6～8 的教訓是「雲端的 IP 跟家裡的 IP 待遇不一樣」（台銀在雲端 60 次全被擋），
而分析只會在雲端 15:30 那一輪算，所以「雲端抓不抓得到、能回溯到哪年、15:30 當下最新一筆是哪天」要先量過再決定接什麼。
**這個停點不接任何來源、不改 assets.json、不動排程、不 commit 任何資料檔。**

**改了什麼**（三個新增檔，全部只讀；第二次合併再加三個文件檔）

| 檔案 | 內容 |
|---|---|
| `scripts/probe_analysis_sources.py` | 新增。對 35 項來源各發一個請求（最多 34 個，台銀 0 個），記狀態（可用／降級／失敗／未測）、回溯到、最新一筆、欄位、備註、失敗指紋、耗時。**永遠 exit 0** 但綠燈不騙人：summary 第一行是四種狀態的統計、每個失敗另發 `::warning::`、測完一項就留一份（中途中斷也留得下已測的）。**「可用」的判準逐項寫死、不看 HTTP 200**：Yahoo 同時檢查宣告的粒度（`meta.dataGranularity`）與時間戳的實際中位間距；FinMind 看 `msg`／`status`（它出錯時 HTTP 照樣 200）；主計總處 API 把「還沒公布的月份回 0.0」當陷阱剔除；ETF 淨值「未結出」算降級。**兩列活的對照列**：`00679B.TW` 必須「失敗」、GC=F `range=max&interval=1wk` 必須「降級」，任一被判「可用」就在表頂端印「探測判準失效」。主機**白名單**（台銀永遠拒絕、就算被加進白名單也擋；轉址逐跳檢查；`https://好主機@壞主機/` 這種寫法不算數）；所有請求固定間隔 3 秒（證交所 3.5）；FinMind 一律不重試、第一個 402／403 整組停手改記未測；只有 Yahoo 的 429 等 10 秒重試一次；15.7MB 的大檔用串流讀到需要的那一段就斷線（上限 400KB）；**永遠不關憑證驗證**；結果只准寫到倉庫以外；輸出不帶電腦名稱與本機路徑；00679B 的 Yahoo 代號讀 `data/assets.json`（`.TWO`）。`--dry-run` 只列請求不連網；`--only` 只測某幾組；`--compare` 把兩個環境的結果並排 |
| `scripts/test_probe_analysis.py` | 新增。**100 條離線測試**，餵假回應、完全不連網：白名單與台銀禁令、間隔、不重試、402／403 停手、串流上限、每一種判準（HTTP 200 但月線／HTML／空結果／缺欄位／太舊／哨兵值／0.0 陷阱／未結出…）、對照列失效要吭聲、永遠 exit 0、結果不准寫進倉庫、公開輸出沒有機器名與路徑 |
| `.github/workflows/probe-analysis.yml` | 新增。只有 `workflow_dispatch`（可選 `only` 輸入）、`permissions: contents: read`、同時只准一個在跑、20 分鐘上限。**第一步跑離線測試，紅就到此為止、不發任何真實請求**；探測步驟 `--summary "$GITHUB_STEP_SUMMARY"`；最後一步 `git status --porcelain` 必須是空的 |
| `.gitignore` | 加 `docs/InvestWatch_分析方法目錄_*.md`：分析方法目錄的原始版本含個人資產配置，留在本機當規格來源、不進公開倉庫（A1 再產去識別化公開版） |
| `docs/CHANGELOG.md`、`README.md` | 這一段；README 的進度、回滾表、檔案結構 |
| 本機 `PLAN.md`（不進倉庫） | 新節 7.12（結果表、三行結論草稿、資料條款、三次探測的時間分開記）；7.8 補放棄的來源；7.11 補「FinMind 當天那一筆幾點出現」的觀察。改之前備份到 `backup/PLAN.md.before-A0` |

**怎麼驗證的**

- **順序規則**：正式探測前先跑離線測試，離線測試紅就停、不打任何真實請求（三次探測都照做；workflow 也是這個順序）。
- 離線測試 **100 條全綠**；全套 **251 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`，既有 151＋新 100）。
- **突變對照 29 個**（工作區複製到暫存目錄→改壞一處→只跑 `test_probe_analysis.py`→必須紅；真倉庫一個位元組不碰）：基準綠，**28 種改壞全部紅**——

  | 改壞的方式 | 結果 |
  |---|---|
  | Yahoo 拿掉「宣告的粒度」這一道 | 2 條紅 |
  | Yahoo 拿掉「實際時間間距」這一道 | 1 條紅 |
  | 兩道都拿掉＝只看 HTTP 200 就說可用 | 4 條紅（含 `test_full_run_states`：對照列 Y-00 被判成可用，`AssertionError: '可用' != '降級'`） |
  | Yahoo 404 的錯誤內容不看了 | 1 條紅 |
  | FinMind 不看 msg／status | 2 條紅 |
  | FinMind 402／403 之後不停手 | 2 條紅 |
  | 台銀那一條禁令拿掉（只剩白名單） | 2 條紅（`HostNotAllowed not raised`） |
  | 白名單不檢查 | 1 條紅 |
  | 轉址不逐跳檢查 | 1 條紅 |
  | 探測程式出錯時回 1 | 1 條紅 |
  | 請求間隔 3 秒改 0.5 秒 | 1 條紅 |
  | 429 重試不限 Yahoo | 1 條紅 |
  | 對照列被判可用也不吭聲 | 2 條紅 |
  | 結果檔可以寫進倉庫 | 1 條紅 |
  | 00679B 用規格寫的 `.TW` | 3 條紅 |
  | FRED 改送瀏覽器 User-Agent | 1 條紅 |
  | 主計總處 XML 把年增率列當成指數 | 3 條紅 |
  | 官方淨值「未結出」也算可用 | 1 條紅 |
  | 大檔不設上限、整檔拉完 | 2 條紅 |
  | FRED 缺值（空字串）硬轉數字 | 2 條紅 |
  | 主計總處 API 解析不出任何月份也不算失敗 | 1 條紅 |
  | 連不上的那一次不算進請求數 | 2 條紅 |
  | 禮貌性的等待算進來源回應時間 | 1 條紅 |
  | 憑證驗不過就 `verify=False` | 1 條紅 |
  | 查網址的那一項照樣報「最新一筆」日期 | 1 條紅 |
  | 主計總處 API 尾端的 0（未公布）當真的數字 | 1 條紅 |
  | 白名單只看網址「長得像」哪一台 | 1 條紅 |
  | 被轉址帶走前已送出的那一跳不計數 | 1 條紅 |

- `--dry-run`：最多 34 個請求；各主機分布如預期；**台銀 0 個**。
- **真實探測**（台北時間；每次都是離線測試綠之後才跑）：

  | 次 | 環境 | 時間 | 結果 | 對照列 |
  |---|---|---|---|---|
  | 1 | 筆電（驗證解析） | 09-21 23:16:45～23:18:42 | 可用 25／降級 3／失敗 2／未測 5，30 次連線（表印 29，連不上那次沒計數，已修） | Y-00 降級 ✔、Y-07 失敗 ✔ |
  | 2 | 雲端 GitHub Actions run 35833365570 | 09-23 15:45:09 由 cron-job.org 觸發、探測 15:46:00～15:47:41 | 可用 23／降級 3／失敗 4／未測 5，30 次 | Y-00 降級 ✔、Y-07 失敗 ✔ |
  | 3 | 筆電（與雲端同時段） | 09-23 15:45 **沒有跑**（S0 待機：任務 20:01 才被啟動、程序 22:36 才執行，包裝腳本在時段外拒跑） | 0 次請求 | — |

  第 1 次實跑抓出探測腳本自己的三個毛病（計時把禮貌性的 3 秒等待算進去、連不上的那一次沒計入請求數、錯誤訊息截掉了真正的原因），
  以及主計總處 API 的「未公布月份回 0.0」陷阱——都補了判準與測試（M21～M28 就是為這些加的對照組）。
  修過的 T-03 判準在 9/21 只用離線樣本驗過，9/23 雲端是第一次對真實端點跑：可用（宣告 549 個月、剔除 2026-09 的 0 之後 548 個月、最新 2026-08＝112.32）。
  兩個環境的差異（`--compare` 雲端 9/23 對筆電 9/21）：狀態不同的只有 **N-01 all_etf.txt 與 N-02 e添富——證交所在雲端回「因為安全性考量，您所執行的頁面無法呈現」**（502／307）；但正式抓取用 `getStockInfo.jsp`＋Referer 在同一種 runner 上每 30 分鐘都成功（9/23 20:08 台股四檔 ok），所以擋的是端點或標頭、不是整站，A1 接之前用正式抓法再測。其餘差異全是「最新一筆」因日期不同。
- 對外請求總數：09-21 共 36 次（完整探測 30＋偵錯 2＋重測 cpi 組 4）；09-23 雲端 30＋筆電 0；**台銀全部 0**。
- 誠實交代：A0 覆述階段（批准前）我對資料端點打了約 100 個請求、還整檔下載了主計總處 15.7MB 的 XML 一次，違反「對來源克制」的精神。
  使用者沒有要補救，但立了新規則：**批准前的偵察只准 (a) 讀本機與倉庫檔案 (b) 讀官方說明文件頁；資料端點留給批准後；大檔先看 Content-Length 或分段讀。**
- 結果本身寫在本機 `PLAN.md` 7.12（含資料條款與三行結論草稿），這裡只留摘要：
  Yahoo 長歷史要用 `period1=0&period2=<now>`（`range=max` 無聲變月線）、00679B 只有 9.7 年；證交所的 all_etf.txt 與 e添富 在雲端被擋（正式抓法沒被擋，A1 再測）；FinMind 各表都夠 10 年但 15:30 拿到的是前一交易日、
  匯率當天那一筆 09-21／09-22 兩天都要到晚上才出現；FRED 三條可用但條款禁止儲存、要誠實 UA；台灣 CPI 用主計總處 API（XML 憑證鏈驗不過）；
  CAPE 只有 multpl.com（★ 級）；淨值長歷史只有 e添富的 00646，00679B 得自己每天累積。

**怎麼退回**

- 第二次合併（文件＋`.gitignore`）：`git revert -m 1 <第二次合併的 commit> && git push`（它是 3782bbe 之後的第一個合併，`git log --oneline --merges -3` 可查）。
- 第一次合併（三個只讀的工具檔）：`git revert -m 1 3782bbe && git push`。沒有任何正式流程引用這三個檔，退回不影響抓取、排程與網站。
- 標籤 `stopA0` 打在分支上第二次合併前的最後一個 commit（`git log --oneline stopA0 -1` 可查）。
