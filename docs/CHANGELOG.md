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
| `stopA1-1` | 資料層＋風險＋拆解：`assetClass`、國際金價、週線長歷史、risk／decompose、檢視頁（A1-5 起併入分析分頁）、公開版方法文件、守門測試 |
| `stopA1-2` | （待排）成本（追蹤差、折溢價、黃金價差、靜態費用表）＋ 個人集中度（只在瀏覽器） |
| `stopA1-6` | 人民幣補齊（週線長歷史、日線回補、存摺價差、拆解）＋卡片「52 週」的說法照實際天數＋換匯助手（fx.json、分批規則表、歷史模擬）＋「一眼看懂」呈現原則 |
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

## 2026-10-01 · 停點 P1 自動駕駛（標籤 `stopP1`；等驗收與第一次實戰後合併）

一個階段裡的例行來回（偵察、覆述、施工、測試、驗收）由 Claude 自己跑完，只在九種情況停下來，每次停下來都寄信；**合併進正式版只有 David 親手輸入「放行 <階段>」才行，而且是程式擋的**。給 David 的白話說明在 `docs/AUTOPILOT.md`。排程、雲端的更新流程（invest-watch 的 workflow）、資料檔一個字沒改；對資料來源的請求 0、台銀 0。

本階段使用模型：claude-fable-5-1（主工作階段從頭到尾都是）／思考強度：max——這個工作階段開在上一層資料夾，P1 的檢查在這裡不會載入，所以不是規定的 Extra high（設定值與實戰那一輪是 Extra high）／中途是否切換：主工作階段否。獨立找漏洞用的是一般的子代理（不是 iw-reviewer），它有一部分回覆是 claude-opus-4-8（平台分配的，不是我指定的）

**先講十二件要請你過目的事**（跟批准的覆述不一樣、或覆述沒寫到而我自己定的）

1. **平常（不在自動駕駛）也有效的規則，比覆述寫的嚴很多。** 第一版是「把危險的寫法列出來、其他放行」。請一個獨立的審查代理（只拿到程式、看不到我的想法）專門找漏洞，第一輪找到 6 類：`git send-pack` 這類不認得的 git 指令（底層的推送，連第二道都不經過）、大括號展開、`$IFS`、`find -exec` 這類幫別人執行的程式、Windows 上同一個檔的其他寫法（結尾的點或空白、`::$DATA`、`\\?\`）、放行詞中間插空白。我照同樣的角度重讀又找到一批（萬用字元、刪掉上層資料夾、資料夾連結、把指令用管線餵給 shell、`cmd //c`、別名、「不一定會執行到」的變數指定、讓 git 另外執行程式的環境變數與選項、全域 git 設定、shell 啟動檔、排程類指令）。現在改成：**看不懂的、不認得的，一律擋**。代價是偶爾會誤擋例行指令——Claude 會換寫法；真的需要時你自己在終端機下（檢查程式只管 Claude）。第二輪（同一個審查代理攻擊修補後的版本）確認第一輪的 6 類都封住了，另外找到 4 類：把「別的指令的輸出」當腳本餵給 shell（`bash <(…)`）、PowerShell 對查到的檔直接呼叫 `.Delete()`、自動駕駛期間用 `python -c` 寫檔或連網、`gh` 的缺口（用 API 建立發行版、從還沒放行的分支執行 workflow）；還有 4 個誤擋（例如在主目錄打包被當成解壓）。都修了。第三輪確認第二輪的 4 類也封住了，結論是「沒有找到新的方法可以動到 main、改寫歷史或移動標籤」；另外找到 3 個較小的缺口與 1 個誤擋，也修了：`git fetch` 可以改掉本機「記著遠端 main 在哪裡」的記號（改到分支的頂端，「這個分支改了哪些檔」就會算成什麼都沒改——現在這種 fetch 會被擋，而且寄「可以合併」的信之前會先向遠端重新問一次）、PowerShell 用 `Start-Process` 去開 `python -c …`、指令藏在 `--選項="git push …"` 裡。審查代理最後的評估：剩下的風險集中在「你放行的那個 commit 本身」與「腳本檔裡面做了什麼」這兩件程式看不到的事。
   連帶改變兩件平常的做法：(a) **自動駕駛期間不能用 `python -c` 或 heredoc 餵程式碼**，只能執行腳本檔與清單裡的模組——檢查程式看不到程式碼裡面做什麼，腳本檔至少留得下來；(b) **`gh workflow run` 不能指定 main 以外的分支**（A0 那種「從分支先跑探測流程」的做法，以後要你自己在 GitHub 網頁上按）——從還沒放行的分支執行流程，等於讓沒審過的流程拿著倉庫的寫入權限跑。
   我自己重讀時還發現第二道的一個洞：合併 commit 的兩個 parent 都對，但合併的時候可以夾帶別的改動。現在第二道會自己算一次「原封不動合併」的結果，內容不一樣就擋；有衝突、手動解過的合併也擋（要停下來寄信）。
2. **P1 自己的實戰原本會卡住**：P1 的分支本來就在改 `.gitignore`（第一層）與自動駕駛自己的檔，照「自動駕駛期間動到就不准寄『可以合併』的信」的規則，P1 的信寄不出去。做法（你已同意）：啟動時記下「啟動之前就已經在分支上的這類變更」（檔名＋內容編號）；內容沒再變就不算自動駕駛動的，但停止報告最後一定用 ⚠ 列出來；自動駕駛期間再動到（連用腳本偷改也算，寄信前比對內容）就寄不出去。不是自動駕駛跑的階段（你在場）動到這些檔：可以登記要合併的版本，但信裡一定寫出來——不然像 A1-1 那種要改 workflow 的階段，Claude 永遠合併不了。
3. **每一句指令詞都事後核對是不是你親手打的**，不只「放行」。hook 收到訊息的當下分不出是誰送的（官方文件沒有這個欄位），所以每一句都先記下來，Claude 做下一個動作之前回頭查對話紀錄。不是你打的＝有東西冒充你：自動駕駛暫停、通行證作廢、寄信。
4. **「動了就停、寄信給你看 diff」的做法**（第二層）：可以改，但你看過 diff（輸入「繼續 <階段>」）之前不能推、不能寄「可以合併」的信；改過之後又改，要再給你看一次（比對 diff 的雜湊）。第二層我多列了 `js/lock.js`、`js/portfolio.js`、`js/records.js`、`records.html`、`js/settings.js`（都是碰私人資料或金鑰的檔）。
5. **`scripts/test_analysis_guards.py` 沒有改**（它自己就是第二層的檔）。覆述寫「掃描清單加新檔」；改成新測試 `test_autopilot_config.py` 直接重用它的掃描器，涵蓋自動駕駛的所有新檔。
6. **黃金排程的例外清單寫死五個檔**（`data/latest.json`、`data/sources/local.json`、三個黃金的歷史檔），用測試釘住「等於 `data/assets.json` 裡 owner 是 local 的標的」——哪天改了 owner，測試會紅，提醒清單要跟著改（改清單要你放行）。
7. **推送前的檢查出錯時**：在 Claude Code 裡＝擋；筆電排程或你自己的終端機＝放行（不可以把既有的排程弄壞）。「規則擋下」與「檢查程式沒跑完」用不同的結束碼分開，連檢查程式本身有語法錯也一樣。
8. **讀檔與搜尋不經過檢查程式**（每讀一個檔都檢查會慢到不能用）。存登入憑證與瀏覽器資料的幾個位置，改用 Claude Code 自己的拒絕規則擋（本站設定頁把 PAT 存在瀏覽器裡）。
9. **每個動作多約 1.2 秒**（覆述寫半秒）：這台電腦開一個程序就要這麼久，加上「任何失敗都當成擋下」的外殼。讀檔與搜尋不受影響。
10. **檔案比覆述多、行數是估計的兩倍多**：多拆出 `iw_common.py`、`iw_shell.py`（把指令拆開）、`iw_events.py`；hook 掛 13 個事件（覆述寫 12 個）；`run.sh` 沒有做（改成設定檔裡一行指令）。行數多主要是測試與第二輪補的規則。
11. **我出的一個包**：流程測試裡模擬「信寄不出去」的那一條，真的在你桌面跳了好幾次 Windows 通知（「自動駕駛：信沒有寄出去／…等你決定要不要合併」），你以為有信寄不出去、有東西等合併。原因是我把寄信換成替身、卻沒有把跳通知也換掉。已修：寄信那一步是替身時只記錄不跳；另有開關讓測試永遠不對外寄信、不跳通知；加了 4 條測試釘住。
12. **擋不住的事照實寫在 `docs/AUTOPILOT.md`**：檢查程式是「看指令的字」來判斷的，不是把 Claude 關在籠子裡。蓄意、分好幾步、把字串藏在腳本裡的做法，程式上不是不可能；真正的底線是第二道（看實際推送的內容）與每封信最後的「安全檢查」。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `.claude/settings.json` | 模型 `claude-fable-5-1`、思考強度 `xhigh`、`switchModelsOnFlag: false`（訊息被標記時暫停，不自動換模型）、不設備援模型；13 個事件的 hook（指令寫成「任何失敗都以結束碼 2 結束」——官方文件：只有 2 會擋）；拒絕清單（常見的危險寫法先擋一層＋憑證位置不准讀）與允許清單（例行指令） |
| `.claude/hooks/iw_guard.py` | **第一道**：每個動作執行前的判斷。永遠有效的一組（沒有通行證不能合併進 main、推 main、強推、刪或移動標籤、改寫歷史；不能寫保護檔、git 的設定與 hook、shell 啟動檔；主目錄只能看與快轉；不認得的 git 指令、看不出執行什麼的寫法、排程類指令一律擋）＋自動駕駛期間多擋的一組（清單以外的工具與程式、第一層的檔、模型或強度不對、超過 8 小時、審查代理以外的子代理、停下之後只能寫報告與寄信） |
| `.claude/hooks/iw_shell.py` | 把一段 Bash／PowerShell／`cmd /c` 指令拆成一個一個實際會執行的指令：引號、`&&`、子殼、`$( )`、heredoc、轉向、變數代回去（只代「一定會執行到」的）、大括號與萬用字元標記、case 樣式。純函式 |
| `.claude/hooks/iw_prepush.py` | **第二道**：git 的推送前檢查，看「實際要推上去的內容」。main：往回退或刪除一律擋；有通行證時必須剛好是「以遠端現在的 main 為底、把放行的 commit 用 `--no-ff` 合併進來」；合併之後只准再推一筆只改 `README.md`／`docs/CHANGELOG.md` 的；沒有通行證只放行黃金排程的形狀。標籤：刪除與移動都擋 |
| `.claude/hooks/iw_events.py` | 各個事件：認你的六句話並開通行證（只有這裡會開）；事後核對指令詞是不是人打的；審查代理的結論與它實際用的模型（hook 記的）；Stop／StopFailure／Notification／SessionEnd 的後援信；工作階段進行中改設定檔不套用；模型被換就暫停；開工作階段時檢查保護檔跟放行過的版本一不一樣 |
| `.claude/hooks/iw_state.py`、`iw_common.py`、`iw_hook.py` | 狀態與通行證（放在 `.git/iw-autopilot/`，不進版控）、對話紀錄的讀法；路徑正規化（含 Windows 的各種同義寫法）、輸出一律 UTF-8；入口（要擋的事件出任何錯都以結束碼 2 結束） |
| `.claude/hooks/iw_notify.py` | 停止報告的格式檢查（固定八段、一頁內、不貼程式碼、最多 3 件要你決定）、觸發通知（`gh workflow run notify.yml -R davidjjx/invest-data`）、寄不出去時寫本機檔＋跳通知＋下次重試、「可以合併」的信寄出前的檢查（標籤、分支推了沒、動不得的檔、你看過 diff 沒、審查紀錄）、每封信最後的模型一行與「安全檢查」一行 |
| `.claude/autopilot/config.json`、`allowlist.json` | 常數（模型、強度、8 小時、通行證 24 小時、文件那一筆 60 分鐘內）、第一層與第二層的檔、黃金排程的例外、自動駕駛期間可以用的工具與程式 |
| `.claude/skills/iw-autopilot/` | `SKILL.md`（Claude 照著做的流程；鐵則與九個停止條件在最前面）、`review-criteria.md`（審查準則：鐵則 23 條、呈現原則 6 條、Cowork 抓過的常見問題 14 條、信的可讀性）、`report-template.md` |
| `.claude/agents/iw-reviewer.md` | 審查代理：`model: claude-fable-5-1`（不用 inherit）、`effort: xhigh`、`tools: Read, Grep, Glob`（唯讀） |
| `scripts/autopilot_install.py` | 裝 `.git/hooks/pre-push`（很短的入口；邏輯檔不在時自動放行，P1 被退回也不會卡住推送）、設定事後偵測的起點、檢查環境；`--bootstrap-from` 是 P1 自己實戰用的（合併前先把保護檔放一份到主目錄被忽略的 `.claude/`） |
| `docs/AUTOPILOT.md`、`docs/notify-workflow.example.yml` | 給 David 的白話說明；私人倉庫用的通知 workflow 範本（機器人開 issue → GitHub 寄信；只有 `issues: write`，不存任何密碼或權杖） |
| `.gitignore` | `.claude/` 從整個忽略改成「只有 settings.json、hooks、agents、skills、autopilot 進版控」；加 `.autopilot/` |
| `scripts/test_autopilot_guard.py`（95 條）、`_prepush.py`（27 條）、`_flow.py`（70 條）、`_shell.py`（30 條）、`_config.py`（23 條） | 全部離線、不跳通知：繞過寫法與例行寫法、推送前的檢查（暫存資料夾裡建假遠端真的 push）、通行證與整個流程、指令解析、設定一致性與隱私掃描 |
| `README.md` | 檔案結構、進度 |

沒有動的：`.github/workflows/`、`data/`（任何資料檔、`schedule.json`、`assets.json`）、`scripts/update_local.ps1`、`scripts/publish.py`、`scripts/fetch_data.py`、`scripts/analyze.py`、`scripts/test_analysis_guards.py`、任何前端檔。

**怎麼驗的**

- **順序**：先在桌面 App 上探測 hook 的實際行為（裝一個只寫紀錄、什麼都不擋的 hook），再施工。探測到、而且改變了做法的事：專案資料夾的 hook 會載入、改設定檔即時生效；結束碼 2 擋、1 不擋；Windows 上中文要自己用 UTF-8 輸出不然是亂碼；代理回報的訊息也會觸發 `UserPromptSubmit`（所以指令詞要整則完全相符，而且事後核對）；對話紀錄慢 0.5～1.5 秒；子代理的動作也經過 hook。
- 全套測試 **785 條全綠**（既有 540＋新 245）：`python -m unittest discover -s scripts -p "test_*.py"`。
- **繞過寫法**（規格要 10 種以上）：覆述列的 32 種（含變形共 58 筆）全部被擋（`TestBypassesAreBlocked`）；三輪審查之後補的 241 筆寫法全部被擋（`TestRound2Bypasses`、`TestRound3`）；79 筆平常會下的例行寫法照樣通過（`TestRound2RoutineWorkStillPasses`、`TestRound3`）——擋得更嚴之後，不可以把正常的工作也擋掉。
- **正向測試**：`test_the_whole_flow_from_start_to_merged`——在暫存資料夾裡真的建倉庫與假遠端：啟動 → 審查代理批准 → 寄「可以合併」的信 → 輸入「放行 X1」→ 合併真的推上去（兩道保護都是真的在跑）→ 文件那一筆通過 → 第三次推 main 被擋。筆電黃金排程形狀的推送不需要通行證；同樣的推送夾帶一個程式檔就被擋。
- **突變對照 75 個**（複製到暫存資料夾 → 把保護改壞一處 → 跑對應的測試 → 必須紅）：5 組基準全綠、**75 種改壞全部紅**（慢的那兩組測試只跑抓得到那個突變的幾條）。其中 4 個第一次沒有紅，處理如下：(1) 「另一個資料流不處理」——測試裡的 `::$DATA` 剛好被另一段程式蓋到，補了「具名的資料流」與「資料夾的另一種寫法」才紅，同時發現並修掉一個真的漏洞（`資料夾:$I30:$INDEX_ALLOCATION` 這種寫法原本認不出來）；(2) 「可以直接在 main 上 commit」——主目錄另有一條規則先擋了，補了「另一個 worktree、但分支是 main」的測試；(3) 「第二道不擋刪除標籤」——刪除同時也被「移動既有標籤」那一條擋住，這個突變其實沒有拿掉保護，改成把整段標籤規則拿掉；(4) 「第二道不核對第二個 parent」——新加的內容比對也擋得住原本那條測試，補了「內容一樣、但不是放行的那個 commit」才紅。

  | 改壞的方式 | 結果 |
  |---|---|
  | 拿掉 git -C 的處理（-C 指到別的資料夾就看不到） | 2 條紅 |
  | 拿掉 PowerShell 工具的檢查 | 12 條紅 |
  | 路徑不正規化（反斜線的寫法就漏掉） | 3 條紅 |
  | 放行詞從「整則完全相符」改成「有提到就算」 | 1 條紅 |
  | 通行證不核對階段 | 2 條紅 |
  | 第一道：通行證不核對要合併的是哪個 commit | 1 條紅 |
  | 通行證可以重複用（合併那一次用過還能再用） | 1 條紅 |
  | 通行證不過期 | 2 條紅 |
  | 第二道：不檢查合併的第二個 parent 是不是放行的那個 commit | 1 條紅 |
  | 第二道：黃金排程的例外從「只動那五個檔」放寬成「有動到那五個檔」 | 1 條紅 |
  | 守門程式當掉時變成放行 | 1 條紅 |
  | 設定檔的 hook 指令少了「／／ exit 2」（找不到 Python 或腳本時變成放行） | 1 條紅 |
  | 模型檢查拿掉（中途被換成別的模型也不擋） | 2 條紅 |
  | 把設定裡的模型改掉 | 1 條紅 |
  | 審查代理改成 inherit | 1 條紅 |
  | 審查代理多給它改檔的工具 | 1 條紅 |
  | Stop 後援不寄信 | 1 條紅 |
  | 「可以合併」的信不檢查審查紀錄 | 1 條紅 |
  | 時間上限不檢查 | 2 條紅 |
  | 清單以外的程式放行 | 1 條紅 |
  | 不認得的 git 子指令放行（git send-pack 就是這樣漏掉的） | 3 條紅 |
  | 不經過推送前檢查的 git 指令（send-pack 這一類）不擋 | 1 條紅 |
  | 開頭看不出是什麼程式（變數、大括號、萬用字元）也放行 | 4 條紅 |
  | 看不懂的指令變回放行 | 3 條紅 |
  | IFS 不擋 | 1 條紅 |
  | 路徑每一段結尾的點與空白不拿掉（.claude. 就漏掉） | 1 條紅 |
  | 另一個資料流（檔名::$DATA）不處理 | 1 條紅 |
  | 「包住保護檔的資料夾」不擋（rm -rf .claude） | 1 條紅 |
  | 萬用字元不看範圍（rm .cl*/hooks） | 1 條紅 |
  | 「不一定會執行到」的變數指定也照代（G=git; false && G=echo） | 1 條紅 |
  | 沒加引號的變數不當成會被拆字 | 1 條紅 |
  | 大括號展開不標出來 | 3 條紅 |
  | 把指令用管線餵給 shell 不擋 | 2 條紅 |
  | 冒充的指令詞不鎖（不是人打的也認） | 2 條紅 |
  | 通行證使用時不回頭核對對話紀錄 | 3 條紅 |
  | 第二道：黃金排程的例外在 Claude Code 裡也放行 | 1 條紅 |
  | pre-push 入口把「檢查程式沒跑完」一律當成擋下（筆電排程會被弄壞） | 1 條紅 |
  | 啟動前就有的變更：自動駕駛中途再改也照樣算「原本就有」 | 1 條紅 |
  | 第一道：啟動前就有的變更不比對內容（推分支時） | 1 條紅 |
  | 測試模式下照樣對外寄信 | 1 條紅 |
  | 測試裡把寄信換成替身時，照樣真的跳桌面通知 | 3 條紅 |
  | 第二道：標籤的刪除與移動不擋 | 1 條紅 |
  | 指令詞中間插空白或零寬字元就認不出來 | 1 條紅 |
  | 思考強度不檢查（Max 也能啟動） | 2 條紅 |
  | 工作階段進行中改設定檔照樣生效 | 1 條紅 |
  | bypass 模式也能啟動 | 1 條紅 |
  | 啟動時不檢查保護檔有沒有被動過 | 1 條紅 |
  | 黃金排程的例外清單多放一個程式檔 | 1 條紅 |
  | 審查代理用的不是規定的模型也算數 | 2 條紅 |
  | 審查兩輪的上限拿掉 | 2 條紅 |
  | 呼叫審查代理可以帶 model 參數 | 1 條紅 |
  | git 的全域設定可以改（hooksPath 可以整個換掉） | 1 條紅 |
  | 會讓 git 另外執行指令的環境變數不擋（GIT_EDITOR、EDITOR） | 1 條紅 |
  | 主目錄可以直接 commit（on main 不擋） | 1 條紅 |
  | --no-verify 不擋 | 1 條紅 |
  | 強推類的選項不擋（第一道） | 3 條紅 |
  | 第二道：讓 main 往回退的推送不擋 | 1 條紅 |
  | 寫入保護檔不擋（改檔工具） | 4 條紅 |
  | 停止狀態下照樣可以做事 | 3 條紅 |
  | 用量上限之後不標成暫停（額度恢復會自己接著跑） | 1 條紅 |
  | 第二道：不比對合併 commit 的內容（合併時可以夾帶別的改動） | 1 條紅 |
  | 第二道：有衝突、手動解過的合併照樣放行 | 1 條紅 |
  | 合併 commit 的例外不看「正在合併的是不是放行的那個 commit」 | 1 條紅 |
  | bash <(指令)、python <(指令)：把別的指令的輸出當腳本執行不擋 | 1 條紅 |
  | PowerShell 對物件呼叫 .Delete() 這類方法不擋 | 1 條紅 |
  | 自動駕駛期間可以執行直接寫在指令裡的程式碼（python -c） | 1 條紅 |
  | gh api 的寫入不擋 | 2 條紅 |
  | 從還沒放行的分支執行 workflow 不擋 | 1 條紅 |
  | 寄「可以合併」之前不看主目錄有沒有被動過 | 1 條紅 |
  | 寄「可以合併」之前不看保護檔跟放行過的版本一不一樣 | 1 條紅 |
  | 解壓縮到包住保護檔的資料夾不擋 | 2 條紅 |
  | 會讓 shell 一啟動就執行別的程式的環境變數不擋（BASH_ENV） | 1 條紅 |
  | 寄「可以合併」之前不重新向遠端問 main 的位置（本機的記號被改就看不到分支改了什麼） | 1 條紅 |
  | git fetch 可以改掉本機記著遠端 main 位置的記號 | 2 條紅 |
  | 指令藏在 --選項="…" 或巢狀引號裡就看不到 | 1 條紅 |

- **規格點名的兩組**：把設定裡的模型改掉 → `test_settings_match_the_config` 紅；偵測那一半是正向測試 `test_model_swap_pauses_mails_and_needs_davids_word`（對話紀錄最新一則回覆的模型不是規定的 → 擋下、暫停、寄出「模型被換成 X，已暫停。要用 X 繼續請輸入『放行模型』；或等額度恢復後輸入『繼續 <階段>』。」）。把審查代理改成 inherit → `test_reviewer_agent_is_pinned_and_read_only` 紅。
- **獨立審查**：同一個獨立的審查代理（一般的子代理；只拿到程式，和一個只會回答「擋／放行」、不會真的執行任何東西的試跑工具）攻擊了三輪：第一輪 6 類、第二輪 4 類＋4 個誤擋、第三輪 3 個較小的缺口＋1 個誤擋；全部修掉並寫成測試。三輪都沒有找到會讓檢查程式當掉的輸入。第三輪的結論：沒有找到新的方法可以動到 main、改寫歷史或移動標籤。
- **實際寄出的測試信**與 **P1 自己的實戰**：見驗收回報（合併之前才會有）。

**怎麼退回**

- `git revert -m 1 <P1 的合併 commit> && git push`，再執行 `python scripts/autopilot_install.py --uninstall`（拿掉 `.git/hooks/pre-push`；不拿也不會卡住——邏輯檔不在時入口自動放行）。退回後 `.claude/` 底下那五項從 main 消失；`.git/iw-autopilot/`（狀態與紀錄）與 `.autopilot/`（報告）不進版控，要的話另外刪。私人倉庫的 `notify.yml` 留著無妨（沒有人觸發就不會跑）。
- 臨時停用（不退回）：用記事本建立 `invest-watch\.claude\settings.local.json`，內容 `{"disableAllHooks": true}`，開新的工作階段；第二道另外用 `python scripts/autopilot_install.py --uninstall`。這兩件事 Claude 自己做不了（都是保護檔），要你動手。
- 標籤 `stopP1` 打在分支上的最後一個 commit（`git log --oneline stopP1 -1` 可查）。

### 實戰前的修正：整段貼上要有提示、測試環境寫死、查版本不誤擋（2026-10-02；標籤 `stopP1` 之後的 commit，同一個合併；標籤不動）

**發生了什麼**　第一次實戰：David 在新的工作階段把「自動駕駛：P1＋規格」**整段貼上**，貼了兩次，畫面都沒有任何反應，自動駕駛沒有啟動。原因：桌面 App 把貼上的長文字包成一個區塊（`<pasted_content …>`），hook 對「以標籤開頭的訊息」一律不認——這是故意的（代理回報、背景通知也走同一個 hook，貼上的內容也不算親手下的指令），但它**一聲不吭**。同一個工作階段重跑全套測試又紅了兩次，都是環境：這台電腦的 `python` 指到 3.13、沒裝 `requests`（478 條裡 2 條失敗、53 條錯誤）；改用 3.12 之後剩瀏覽器那 11 組一開始就錯（650 條裡 11 個錯誤、135 條沒跑到）——測試找到的是 Edge，它的無頭模式回空白頁。

David 的裁決：用一般模式（不是自動駕駛）在 P1 分支修三件事——(1) 整段貼上不准靜悄悄沒反應；(2) 測試環境寫死，不靠記憶，查版本這類唯讀指令不要擋；(3) 通知信先打通。修完他再手打「自動駕駛：P1」啟動實戰。

本階段使用模型：claude-fable-5-1／思考強度：一般模式，自動駕駛沒有啟動，hook 不檢查強度，無法從紀錄驗證／中途是否切換：否。

**先講五件要請你過目的事**（裁決沒寫到、我自己定的，或施工時多發現的）

1. **提示的範圍比「貼上的區塊」大，但有邊界。** 三種情況會提示：指令詞在貼上的區塊裡；單獨一行是指令詞、但不在第一行或前後還有別的行（`放行 P1` 下面又寫一行）；寫法差一點（`放行P1`、`放行 P1 謝謝`、`自動駕駛 P1`、一句話以內的「請幫我 放行 P1」）。不提示的：代理回報與背景通知這類機器包起來的訊息（不然審查代理每回報一次就跳一行）、超過 60 個字的文件裡順便提到、「繼續」「修改」開頭的平常講話（只有後面接著現在這個階段的名稱才提示）。提示只有一行字：不啟動、不放行、不改狀態，測試釘住「提示之後照樣推不上 main、照樣沒有通行證」。
2. **多修了一個漏洞（裁決沒有要求）。** 查「為什麼查版本被擋」的時候看到：守門對 python 的規則是「參數裡有 `--version` 就整段不檢查」。`python -c "…" --version` 裡的 `--version` 只是那段程式自己的參數，程式碼照樣會執行——等於加一個字就跳過「寫在指令裡的程式碼」的檢查；自動駕駛期間還能跳過「只能執行清單裡的模組、專案裡的腳本」。現在只認排在腳本、`-c`、`-m` **前面**的版本與說明選項。這是把保護收緊，不是放寬。
3. **查版本被擋的真正原因不是 python，是 `command -v`。** `command -v py` 只是問「py 是哪個程式」，守門把它當成「用 command 去執行 py」，於是說「py 沒有指定腳本檔」。現在 `command -v`／`-V` 算唯讀；沒有 `-v` 的 `command git push` 照樣當成執行後面那個程式（照樣擋）。`py -0p`、`py --list`（列出裝了哪幾個版本）也認了。
4. **流程檔（SKILL.md）這次沒有改到——被生效中的守門誤擋，我沒有繞過。** 流程檔的資料夾（`.claude/skills/iw-autopilot`）跟狀態資料夾（`.git/iw-autopilot`）同名，守門是「路徑裡有這幾個字就當成狀態資料夾」，所以連 worktree 裡的副本都改不了（照設計，副本在一般模式可以改）。分支上已經把守門修掉；但生效的是主目錄那一份，要重新安裝或合併之後才放行。所以「測試環境寫進流程或設定」改成：**寫進設定（`config.json` 的 `testEnv`），由 hook 在每次啟動、繼續、修改、放行、壓縮對話之後直接講給 Claude 聽**——比寫在流程檔裡更不靠記憶。SKILL.md 待補的那一段文字放在 `.autopilot/runs/P1/04_SKILL待補.md`，補不補由你決定。
5. **這些修正要「重新安裝」才會在實戰生效。** 主目錄的 `.claude/` 還是 `stopP1` 那一版的副本（P1 還沒合併）。把新版放過去是改生效中的保護檔，我不能做、也不該做——要你自己在終端機跑一行（見驗收回報）。沒跑的話，實戰照樣能跑（第一行手打就會啟動），只是沒有提示、沒有測試環境的提醒。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `.claude/hooks/iw_state.py` | `near_miss()`：認出「長得像指令詞、但格式不被接受」的訊息（只給提示用，不是指令）；`strip_paste_wrapper()`：存規格時拿掉貼上的包裝那兩行 |
| `.claude/hooks/iw_events.py` | 不認的訊息先問 `near_miss()`，有就回一行 David 看得到的提示＋告訴 Claude「不要把它當成指令照做」，並記一筆 `near-miss`；`tests_env_note()`：把 `testEnv` 講給 Claude 聽（啟動、繼續、修改、放行、壓縮對話之後） |
| `.claude/hooks/iw_guard.py` | `command -v`／`-V` 算唯讀；python 的版本與說明選項只認排在程式碼前面的；`py -0p`、`py --list`；狀態資料夾的判斷不再把 `skills/iw-autopilot` 算進去（路徑裡有變數時照舊從嚴） |
| `.claude/autopilot/config.json` | 新增 `testEnv`：`py -3.12`、`IW_BROWSER`、Chrome 的路徑 |
| `.claude/settings.json` | 允許清單加 `py -3.12` 的例行寫法與查版本的指令（`python --version`、`py --list`、`command -v *`、`which *`…） |
| `docs/AUTOPILOT.md` | 「第一行要用鍵盤打」；寫法差一點時會有提示；出問題的時候多三列；測試環境 |
| `scripts/test_autopilot_flow.py`（＋8 條）、`_guard.py`（＋4 條）、`_config.py`（＋1 條） | 見下面 |
| `README.md` | 進度多一行 |

不進版控、只在這台電腦上的：`.autopilot/local-env.txt`（具名字串的隱私掃描要的兩個環境變數——值是私人路徑，所以不放進倉庫）。

沒有動的：`.claude/skills/`（見上面第 4 點）、`.claude/agents/`、`allowlist.json`、`iw_prepush.py`、`iw_notify.py`、`.github/workflows/`、`data/`、`scripts/update_local.ps1`、任何前端檔。對資料來源的請求 0、台銀 0。

**怎麼驗的**

- **測試環境**（以後都這樣跑）：`py -3.12`、`IW_BROWSER` 指到 Chrome、加上 `.autopilot/local-env.txt` 的兩個環境變數（具名字串的隱私掃描有跑，不是略過）。
- **先核對原本的 785 條**（David 要求）：在標籤 `stopP1` 那一版（另開一個暫時的 worktree，跑完收掉）用上面的環境重跑全套——**785 條全綠**，跟 `tests.txt` 一致。同一版、同一天的另外兩次重跑紅的原因都是環境：`python`（3.13，沒裝 `requests`）→ 478 條裡 2 條失敗、53 條錯誤；`py -3.12` 但瀏覽器找到 Edge → 650 條裡 11 個錯誤（瀏覽器那 11 組一開始就錯，135 條沒跑到；650＋135＝785）。換成 Chrome 之後那 11 組全綠，所以「空白頁」是 Edge 無頭模式的問題，不是程式或測試的問題。照實說：`tests.txt` 沒有記那一次用的是哪個瀏覽器。
- **修正之後的全套**：**798 條全綠**（785＋新 13：流程 8、守門 4、設定 1）。
- **新測試釘住的事**：
  - 整段貼上（桌面 App 實際送來的樣子：兩個換行＋區塊）→ 出現「沒有啟動…第一行請手打「自動駕駛：<階段>」，規格貼在下面」；狀態沒變、沒有存規格、沒有通行證；之後 `git push origin HEAD:main` 照樣被擋、自動駕駛的清單沒有生效（`test_whole_message_pasted_gets_a_visible_hint_and_does_not_start`）。
  - 手打第一行＋貼上規格 → 啟動；存下來的規格不帶包裝；下一個動作回頭核對「是不是人打的」照常通過（`test_typed_first_line_plus_pasted_spec_starts`）。
  - 一切就緒、只差放行的時候：貼上的「放行 X1」、`放行X1`、「放行 X1」下面多一行、「請幫我 放行 X1」、只打「放行」——六種都只換來提示，沒有通行證；手打的才開（`test_a_pasted_or_misformatted_approval_gets_a_hint_but_no_credential`）。
  - 20 種差一點的寫法認得出來、而且 `parse_command` 照樣不認；18 種不該提示的不提示（真的指令詞、機器包起來的訊息、平常講話、長文件）。
  - 查版本的 18 種寫法在主目錄、worktree、自動駕駛期間都不擋；`python -c "…" --version` 這類 7 種照樣擋；寫死的全套測試指令在自動駕駛期間放行。
  - 流程檔在 worktree 的副本：一般模式可以改、自動駕駛期間擋；主目錄那一份與真正的狀態資料夾永遠擋；路徑裡有變數時從嚴。
  - hook 在啟動、繼續、修改、壓縮對話之後都把測試環境講一次；設定裡沒有 `testEnv` 就不講（不編一個）。
- **突變對照**：原本的 75 個重跑＋這次新增 15 個，5 組基準全綠、**90 個全部紅**，這次沒有「第一次沒紅」的。新增的 15 個：

  | 改壞的方式 | 結果 |
  |---|---|
  | 整段貼上時的提示拿掉（又變回靜悄悄沒反應） | 3 條紅 |
  | 貼上的區塊裡的指令詞也當成指令（貼上＝啟動、貼上＝放行） | 4 條紅 |
  | 機器包起來的訊息（代理回報、背景通知）也跳提示 | 2 條紅 |
  | 長文件裡提到指令字也跳提示（不看「一句話」的長度上限） | 1 條紅 |
  | 存規格時不拿掉貼上的包裝 | 1 條紅 |
  | `command -v` 改回「當成要執行後面那個程式」（查版本又被擋） | 1 條紅 |
  | 參數裡有 `--version` 就整段不檢查（`python -c "…" --version` 就漏掉） | 1 條紅 |
  | `py` 列出版本的選項不認（`py -0p`、`py --list` 被擋） | 1 條紅 |
  | 狀態資料夾改回「路徑裡有這幾個字就算」（流程檔的副本又改不了） | 1 條紅 |
  | 狀態資料夾的例外放寬成「路徑裡有 skills 就不算」 | 1 條紅 |
  | 路徑裡有變數時不從嚴 | 1 條紅 |
  | 啟動時 hook 不講測試環境（又變回靠記憶） | 1 條紅 |
  | 設定裡的測試用 Python 改回 `python` | 2 條紅 |
  | 設定裡的測試用瀏覽器改成 Edge | 2 條紅 |
  | 允許清單拿掉查版本的指令 | 1 條紅 |
- **通知信**：私人倉庫的 `notify.yml` 確認在（`gh workflow view`）；2026-10-02 10:21 寄出第二封測試信，GitHub 上那一次執行 12 秒、成功。David 有沒有收到：見驗收回報。
- **誤擋的紀錄**（照實）：這個工作階段被守門擋了 4 次——兩次是 `command -v`（已修）、兩次是改 SKILL.md（已在分支上修；我沒有換別的方法做同一件事）。

**怎麼退回**

- 合併前：這是標籤之後的 commit。只退這一筆：`git revert <這一筆的 commit>`（在分支上）；退回後整段貼上又會靜悄悄沒反應、查版本又會被擋、`python -c "…" --version` 的漏洞回來。
- 合併後：跟 P1 一起退（上面的 `git revert -m 1 <P1 的合併 commit>`）。
- `.autopilot/local-env.txt` 不進版控，不要了直接刪。

## 2026-09-30 · 分析系列 A1-7 小白呈現框架（標籤 `stopA1-7`；2026-10-01 合併 54dd3f9）

**合併紀錄（2026-10-01）**

- 驗收通過；六件裁決與裁決後的修正見本節最後的「驗收後的修正」。
- 合併 commit **54dd3f9**（`--no-ff`；第一個 parent 是 main 的 4e49c64，第二個是分支的 e11f44c）。合併在另一個暫時的 worktree 做，主目錄留在 main、排程照跑。
- 沒有衝突：A1-7 沒有動任何資料檔，main 這段期間只有資料 commit。第一次合併好、要推之前，雲端剛好又推了一筆資料（08:49 那一輪），所以改以新的 main 為底重做一次合併、重跑測試再推；沒有用強推。
- 合併後的樹上全套測試 540 條全綠（兩次合併各跑一次）。
- 合併後的這一筆文件 commit 只填 README 的回滾表與這一段紀錄，沒有動程式。

把 A1-6 換匯助手的「圖示＋狀態詞＋規則＋白話＋名詞解釋」推到所有卡片與整個分析分頁，分析分頁最上面加一張總覽表。不多算新指標、不改排程、不加對外請求；原本畫面上的東西一項不減。

（這一節寫的是 tag `stopA1-7` 那一版。驗收的六件裁決與裁決後的修正在本節最後的「驗收後的修正」。）

**先講六件要請你過目的事**

1. **有一處不是純前端。** 裁決要折溢價「跟自己歷史的第 25／75 百分位比」，但 `cost.json` 的折溢價摘要只有中位數、最小、最大。所以 `scripts/analyze.py` 的摘要多輸出 `p25Pct`／`p75Pct` 兩個欄位（線性內插；跟中位數一樣滿 20 個交易日才有）。現在兩檔 ETF 都只累積 3 天，這一輪的資料檔一個字都沒有變；不加請求、不動排程、不動 workflow。
2. **折溢價的狀態先看「確定」口徑**（收盤對官方淨值，資料標籤「單一來源」；預估口徑是「估算」），預估並列在細節裡。現在兩個口徑都不滿 20 天，畫面上都是「資料不足　累積中 3/20」，看不出差別；等 A1-4 的口徑裁決。
3. **摺疊區的小字改了一句。** 「分析」旁邊原本寫「風險、相關、成本；展開才讀取」，現在資料是首屏畫完就抓，那句話不再是真的，改成「風險、相關、成本的完整數字」。改前改後比對裡，畫面上消失的字只有這一句。
4. **儀表板程式 `js/app.js` 這次納入守門掃描**（以前不在清單裡）。它原本就有一個價格的名字——條塊卡上說明「比存摺貴」是跟黃金存摺的哪個牌價比的那個詞——會被擋字串擋下。我沒有改那句既有的字，只在這一個檔豁免這一個詞（別的寫法照擋，有測試）。要不要改成把那句字換掉，請你決定。
5. **這一輪自己定的幾條**（規格沒有給）：
   - 成本「不適用」的是指數、股票、加密貨幣、商品，各有一句原因。不知道是哪一類的標的不說不適用：檔案讀不到寫「暫時讀不到」，檔案裡還沒有它寫「資料不足」。
   - 條塊的成本看 1 公斤那一列，跟本站自己累積的天數的中位數比，滿 20 個交易日才比（現在 14 天，寫「累積中 14/20」）。存摺的中位數是手上有的天數（現在 264 天），規則句照實寫天數、不寫「一年」。
   - 最大回檔那句白話原本只有「大約一半／三分之一／五分之一」三種說法（A1-6 只用在匯率）；推到全部標的之後補成八種（八成以上、四分之三、三分之二、一半、四成、三分之一、四分之一、五分之一），不然跌 88% 的會被說成「大約一半」。
   - 總覽表同一個狀態照名稱排，用的是瀏覽器的繁體中文排序（中文字排在英文字母前面）。
   - 換匯助手「人民幣現在」那一格的內部名稱從 `position` 改成 `fxPosition`（第 6 節的表拆成三列之後，位置、匯率位置、估值各有各的名字），畫面上的字沒有變。
6. **相關的那一句裡，S&P 500 指數「最常同方向」的是它自己的總報酬版本（1.00）。** 這是 A1-5 就有的行為（卡片摺疊區的「最同向」也是它）——總報酬指數是追蹤差用的基準、也在相關矩陣裡。這一輪照原樣寫成白話，沒有改規則。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `js/plain.js` | 位置的狀態（`lampState`）：直接拿燈號函式輸出的那幾個字，不重算、不換字，圖示用色點；匯率位置改名 `fxPositionState`；折溢價的狀態 `premiumState`（跟自己歷史的第 25／75 百分位比，不滿 20 個交易日是資料不足＋「累積中 n/20」）；成本的狀態可以指定比的是什麼與規則句；三種沒有狀態分開：`insufficient`（資料不足）、`notApplicable`（不適用）、`unavailable`（暫時讀不到）。白話模板從 9 種加到 26 種（位置、跟平常比、1 年相對 5 年、目前距高點、相關、最像的幾檔、追蹤差、折溢價與它在自己歷史裡的排名、累積中、條塊溢價、拆解的殘差、人民幣變動的兩段、費用、集中度的三個事實）。純函式 |
| `js/glossary.js` | 名詞加 14 個：面向、狀態詞、保底、視窗重疊，與 RSI、移動平均、近 10 日漲跌、年化波動、區間位置、目前距高點、殘差、淨值、資料標籤、條塊溢價；`upgrade()` 把先畫出來的字換成可以點的按鈕；點名詞不往外傳（放在可排序的表頭裡也不會觸發排序） |
| `js/card-analysis.js` | 每張卡的面向 `aspects()`：位置（燈號）、風險（1 年波動相對 5 年）、成本（存摺價差、條塊溢價、換匯價差、折溢價；沒有這個面向的類別是不適用）；圖示列 `strip()`、點開的細節 `detail()`、總覽表的格子 `cell()`。匯率卡的位置細節多一行「5 年位置：…（見換匯助手）」。「分析」摺疊區每一列下面一行白話、相關那一組一句。仍然是純函式 |
| `js/app.js` | 每張卡在標題列與展開區之間多一條圖示列（首屏先留一行「讀取中」）；白話層、名詞解釋與三個分析檔等首屏畫完（每張卡的日線都到了）才抓，圖示列與摺疊區共用；指標格與區間位置的標題變成可以點的名詞；分析檔讀不到時摺疊區照實寫；按「重新讀取」三個分析檔也重讀 |
| `js/analysis.js`、`analysis.html` | 最上面一行分析狀態＋總覽表（每個標的一列、每個面向一格；預設順序跟儀表板一樣、可照某一欄排序、可回到預設；點一列展開細節；手機橫向捲動、第一欄固定；沒有任何計數）；開頁多讀 `data/latest.json` 與每個標的的日線；風險表、成本、拆解、集中度、試算的每個數字下面一行白話，表頭與小標的名詞可以點；相關矩陣下面每個標的一句；逐日／逐週／逐月明細的標題寫明「白話寫在上面的摘要」；試算結果多一條圖示列（風險、最像的三檔） |
| `css/style.css` | 只新增圖示列與細節的樣式（`.card-glance`、`.asp*`、`.glance-note`），不改既有選擇器 |
| `scripts/analyze.py`、`scripts/test_analyze.py` | 折溢價摘要多 `p25Pct`／`p75Pct`（`quantile()`，線性內插）；測試 2 條 |
| `scripts/test_aspects.html`／`test_aspects_js.py`（新） | 面向 15 題＋接線 4 條：位置跟燈號一字不差（跟 A1-5 fixture 同樣的 30 組輸入）、風險與成本的門檻、折溢價的規則與口徑、不適用、匯率卡的連結、圖示與細節、讀不到、沒有禁用詞與計數、摺疊區的白話 |
| `scripts/test_dashboard_live.html`／`test_dashboard_requests_js.py`（新） | 真的開瀏覽器量儀表板：測試自己起一個只聽 127.0.0.1 的小伺服器，17 條——首屏前後的請求數（瀏覽器與伺服器兩份紀錄對照）、每張卡的圖示列、位置等於燈號、指標格的名詞、分析檔或白話層抓不到時的樣子 |
| `scripts/test_plain.html`／`_js.py`、`scripts/test_analysis_page.html`／`_js.py` | 白話層 8 → 12 題（每一種模板至少三組：正常、極端、資料不足，逐字比對）；分析分頁 28 → 38 題（總覽表 6 題、各區白話、表頭名詞、相關的每標的一句、試算圖示列）；接線測試改成「首屏畫完才抓」的結構 |
| `scripts/test_analysis_guards.py` | 掃描清單加四個新測試檔與 `js/app.js`；`js/app.js` 既有的那一個價格名稱只在那個檔豁免 |
| `docs/ANALYSIS.md`、`README.md` | 新增 5.11「小白呈現框架」；第 6 節的表拆成位置／匯率位置／估值三列，補「資料不足、不適用、暫時讀不到」的分別；第 7 節更新 |
| 各 HTML | `bump_assets.py` 版本 20260930-8 |

沒有動的：`.github/workflows/`、`data/schedule.json`、`scripts/fetch_data.py`、`scripts/report.py`、`js/indicators.js`（燈號的函式一個字沒改）、任何資料檔。

**怎麼驗的**

- 單元測試 **537 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`；頁面測試用 Chrome）。A1-5 的燈號 fixture 仍綠。
- **首屏前後的請求數**（`python scripts/test_dashboard_requests_js.py --measure`，無頭瀏覽器實開、資料是倉庫裡的檔案）：

  ```
  儀表板請求數量測（無頭瀏覽器實開；資料是倉庫裡現在的檔案）
    首屏畫完之前：23 個請求＝靜態檔 7 個＋資料檔 16 個（行情 1、報告 1、日線 14）
      css/style.css
      data/history/btc.json
      data/history/fx_cny.json
      data/history/fx_usd.json
      data/history/gold_bar.json
      data/history/gold_cny.json
      data/history/gold_intl.json
      data/history/gold_twd.json
      data/history/gspc.json
      data/history/nvda.json
      data/history/tw00646.json
      data/history/tw00679b.json
      data/history/tw2330.json
      data/history/twii.json
      data/history/wti.json
      data/latest.json
      data/report-latest.json
      js/app.js
      js/card-analysis.js
      js/charts.js
      js/freshness.js
      js/indicators.js
      lib/chart.umd.min.js
    首屏畫完之後：5 個請求（上限 5）
      data/analysis/cost.json
      data/analysis/fx.json
      data/analysis/risk.json
      js/glossary.js
      js/plain.js
    點開一張卡、展開「分析」之後：總數 28（沒有再多抓）
    伺服器收到的順序：延後的第一個排第 24，首屏的最後一個排第 23（共 28 個）
    對外連線：0（全部打 127.0.0.1）
  ```

- **改前改後比對**（同一份資料；改前＝分支起點 24c737e）。DOM 逐行比（一個標籤一行，隨機的漸層 id、版本號、快取參數先正規化）：

  ```
  #### dashboard.html：before 698 行 → after 2360 行；少了 1 行、多了 1663 行
  #### card_tw00646.html：before 180 行 → after 360 行；少了 7 行、多了 187 行
  #### card_gold_twd.html：before 131 行 → after 244 行；少了 7 行、多了 120 行
  #### card_gold_cny.html：before 131 行 → after 244 行；少了 7 行、多了 120 行
  #### card_gold_bar.html：before 150 行 → after 247 行；少了 1 行、多了 98 行
  #### card_fx_cny.html：before 177 行 → after 343 行；少了 7 行、多了 173 行
  #### card_fx_usd.html：before 177 行 → after 343 行；少了 7 行、多了 173 行
  #### card_tw2330.html：before 165 行 → after 283 行；少了 7 行、多了 125 行
  #### card_twii.html：before 165 行 → after 287 行；少了 7 行、多了 129 行
  #### card_btc.html：before 159 行 → after 277 行；少了 7 行、多了 125 行
  #### analysis.html：before 1937 行 → after 4119 行；少了 39 行、多了 2221 行
  合計：少了 97 行、多了 5134 行
  ```

  「少了」的行都是同一段字換了包法——外面多包一個名詞按鈕、同一格裡多一行白話、元素多一個屬性——所以另外做了**文字層比對**（把標籤拿掉，改前的每一段字在改後出現的次數不可以變少）：

  ```
  dashboard.html         改前  218 段字（  2437 字）→ 改後  854 段（ 11917 字）；改後少了的：0 段
  card_tw00646.html      改前   84 段字（   927 字）→ 改後  162 段（  2156 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_gold_twd.html     改前   53 段字（   510 字）→ 改後   88 段（  1048 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_gold_cny.html     改前   53 段字（   523 字）→ 改後   88 段（  1076 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_gold_bar.html     改前   71 段字（   762 字）→ 改後  104 段（  1392 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_fx_cny.html       改前   83 段字（  1010 字）→ 改後  153 段（  2059 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_fx_usd.html       改前   85 段字（  1027 字）→ 改後  155 段（  2086 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_tw2330.html       改前   75 段字（   831 字）→ 改後  123 段（  1633 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_twii.html         改前   75 段字（   856 字）→ 改後  123 段（  1668 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  card_btc.html          改前   71 段字（   772 字）→ 改後  119 段（  1549 字）；改後少了的：1 段
      改前 1 次、改後 0 次：風險、相關、成本；展開才讀取
  analysis.html          改前 1728 段字（ 17240 字）→ 改後 2560 段（ 31132 字）；改後少了的：0 段
  合計：改前 2596 段字，改後少了 9 段
  ```

  改後少了的 9 段是同一句：每張展開的卡各一次的「風險、相關、成本；展開才讀取」（上面第 3 件事）。
- **突變對照組 73 組全部符合預期**（1 組基準綠、其餘把程式改壞都紅；★ 是規格第 7 節的八組與驗收追加的三組，R 是這一輪新邏輯的補充）：

  | 改壞的方式 | 結果 |
  |---|---|
  | （基準）什麼都不改：全套測試 | 綠（537 條全過） |
  | ★1a 圖示沒有狀態詞（卡片的圖示只剩圖示與面向名稱）→ 面向的離線測試 | 1 條紅：test_every_chip_has_icon_name_and_word |
  | ★1b 同上 → 真的開儀表板量的那一組 | 3 條紅：test_every_chip_has_icon_name_word_and_a_detail_with_a_rule、test_position_chip_is_the_lamp_word_for_word、test_chips_say_temporarily_unavailable |
  | ★1c 圖示沒有狀態詞（「一眼看懂」那一格不印狀態詞） | 2 條紅：test_icon_always_has_its_word、test_icon_always_prints_its_rule |
  | ★1d 圖示沒有狀態詞（總覽表的格子只有圖示） | 1 條紅：test_overview_cells_are_the_card_aspects |
  | ★2a 圖示沒有規則（點開的細節不印規則）→ 面向的離線測試 | 1 條紅：test_every_detail_prints_rule_and_plain_lines |
  | ★2b 同上 → 總覽表展開的那一列 | 1 條紅：test_overview_row_expands |
  | ★2c 同上 → 真的開儀表板量的那一組 | 1 條紅：test_every_chip_has_icon_name_word_and_a_detail_with_a_rule |
  | ★2d 圖示沒有規則（「一眼看懂」那一格不印規則） | 1 條紅：test_icon_always_prints_its_rule |
  | ★3a 白話模板輸出空字串（資料不足時模板回空字串、保底的那一層也拿掉） | 2 條紅：test_every_template_has_three_kinds_of_cases、test_insufficient_data_is_said_out_loud |
  | ★3b 白話模板輸出空字串（資料不足時那一行印成空的）→ 白話層的測試 | 2 條紅：test_every_template_has_three_kinds_of_cases、test_insufficient_data_is_said_out_loud |
  | ★3c 同上 → 分析分頁每一區的白話 | 2 條紅：test_every_section_has_plain_lines、test_fx_position_and_cost_tables |
  | ★3d 白話模板對正常的數字也輸出空字串（追蹤差那一句） | 1 條紅：test_every_template_has_three_kinds_of_cases |
  | ★4a 出現禁用詞（成本的狀態詞換成帶方向的字）→ 白話層的測試 | 4 條紅：test_icon_always_has_its_word、test_no_trading_words_no_counting、test_premium_state_uses_own_quartiles、test_three_states_follow_the_rules |
  | ★4b 同上 → 守門的擋字串 | 1 條紅：test_no_judgement_words_in_analysis_files |
  | ★4c 出現禁用詞（圖示的文字替代裡）→ 面向的離線測試 | 1 條紅：test_no_trading_words_no_counting |
  | ★4d 同上 → 真的開儀表板量的那一組 | 1 條紅：test_no_trading_words_and_no_counting_on_the_strips |
  | ★4e 出現禁用詞（寫在儀表板程式 js/app.js 裡；這一輪才納入掃描） | 1 條紅：test_no_judgement_words_in_analysis_files |
  | ★5a 總覽表顯示計數（表格下面多一句「N 個標的在相對高檔區」） | 1 條紅：test_overview_never_counts |
  | ★5b 卡片的圖示列顯示計數（「N 個面向」）→ 面向的離線測試 | 2 條紅：test_no_trading_words_no_counting、test_unreadable_files_say_so |
  | ★5c 同上 → 真的開儀表板量的那一組 | 2 條紅：test_every_card_has_a_strip_between_head_and_body、test_no_trading_words_and_no_counting_on_the_strips |
  | ★6a 位置面向的字眼被改成偏貴（位置拿區間百分位去套匯率位置那一套）→ 面向的離線測試 | 5 條紅：test_aspects_order_word_number_rule、test_fx_cards_keep_the_lamp_and_link_to_the_helper、test_no_trading_words_no_counting、test_position_is_the_same_lamp_as_the_card、test_position_never_recomputes_the_lamp |
  | ★6b 同上 → 總覽表 | 1 條紅：test_overview_cells_are_the_card_aspects |
  | ★6c 同上 → 真的開儀表板量的那一組（位置的字要跟名稱旁邊的燈號一樣） | 3 條紅：test_every_chip_has_icon_name_word_and_a_detail_with_a_rule、test_position_chip_is_the_lamp_word_for_word、test_chips_say_temporarily_unavailable |
  | ★6d 位置面向的字眼被改成偏貴（白話層不拿燈號的字，自己查「偏便宜／中間／偏貴」的表） | 1 條紅：test_position_state_is_the_pinned_lamp_word_for_word |
  | ★6e 位置的圖例被改成偏貴（plain.js 抄的那一份三個詞） | 2 條紅：test_icon_always_has_its_word、test_position_state_is_the_pinned_lamp_word_for_word |
  | ★7a 首屏請求數超過 23（把白話層掛回 index.html 的靜態標籤）→ 真的量請求數 | 3 條紅：test_first_screen_requests_are_exactly_the_static_files_and_one_file_per_asset、test_server_saw_the_same_requests_in_the_same_order、test_first_screen_is_untouched |
  | ★7b 同上 → 只看原始碼的接線測試 | 1 條紅：test_dashboard_first_screen_loads_seven_static_files_and_defers_the_rest |
  | ★7c 首屏請求數超過 23（一開頁就去抓三個分析檔）→ 真的量請求數 | 4 條紅：test_at_most_five_more_requests_after_the_first_screen、test_first_screen_requests_are_exactly_the_static_files_and_one_file_per_asset、test_server_saw_the_same_requests_in_the_same_order、test_first_screen_is_untouched |
  | ★7d 同上 → 只看原始碼的接線測試 | 1 條紅：test_dashboard_first_screen_loads_seven_static_files_and_defers_the_rest |
  | ★7e 不等日線到齊就去抓延後的五個檔（首屏還沒畫完就插隊）→ 真的量請求數 | 3 條紅：test_at_most_five_more_requests_after_the_first_screen、test_first_screen_requests_are_exactly_the_static_files_and_one_file_per_asset、test_chips_say_temporarily_unavailable |
  | ★8a 燈號 fixture 改壞（fixture 裡一組的燈號被改掉） | 1 條紅：test_signals_and_indicators_match_the_fixture_made_from_main |
  | ★8b 燈號的門檻被改（25 改成 30）→ 釘住的 fixture | 1 條紅：test_signals_and_indicators_match_the_fixture_made_from_main |
  | ★8c 燈號自己的字被改成偏貴 → 釘住的 fixture | 1 條紅：test_signals_and_indicators_match_the_fixture_made_from_main |
  | ★8d 同上 → 白話層（位置的三個詞要等於燈號的三個字） | 1 條紅：test_position_state_is_the_pinned_lamp_word_for_word |
  | ★9a 折溢價用錯規則（拿去跟中位數比一成，不是跟第 25／75 百分位比） | 1 條紅：test_premium_uses_own_quartiles_and_the_official_row |
  | ★9b 折溢價用錯規則（第 25／75 百分位對調） | 1 條紅：test_premium_state_uses_own_quartiles |
  | ★9c 折溢價的狀態看成「預估」口徑 | 1 條紅：test_premium_uses_own_quartiles_and_the_official_row |
  | ★9d 後端把第 25／75 百分位算反 | 1 條紅：test_premium_summary_has_quartiles_only_after_twenty_days |
  | ★9e 「累積滿 20 個交易日」的門檻改成 2 | 3 條紅：test_bar_cost_waits_for_twenty_days、test_every_chip_has_icon_name_and_word、test_premium_uses_own_quartiles_and_the_official_row |
  | ★10a 不適用被寫成資料不足（指數、股票的成本）→ 面向的離線測試 | 1 條紅：test_not_applicable_is_not_insufficient |
  | ★10b 同上 → 總覽表 | 2 條紅：test_overview_cells_are_the_card_aspects、test_overview_row_expands |
  | ★10c 不適用被寫成資料不足（白話層把兩種狀態做成同一種） | 1 條紅：test_insufficient_and_not_applicable_are_different |
  | ★10d 卡片上把「不適用」也畫成一個圖示 → 面向的離線測試 | 2 條紅：test_every_chip_has_icon_name_and_word、test_not_applicable_is_not_insufficient |
  | ★10e 同上 → 真的開儀表板量的那一組 | 2 條紅：test_every_chip_has_icon_name_word_and_a_detail_with_a_rule、test_not_applicable_is_never_drawn_on_a_card_and_fx_cards_link_to_the_helper |
  | ★11a 首屏之後超過上限（多抓一個 decompose.json，變成 6 個）→ 真的量請求數 | 3 條紅：test_at_most_five_more_requests_after_the_first_screen、test_server_saw_the_same_requests_in_the_same_order、test_first_screen_is_untouched |
  | ★11b 同上 → 只看原始碼的接線測試 | 1 條紅：test_dashboard_first_screen_loads_seven_static_files_and_defers_the_rest |
  | ★11c 首屏之後超過上限（多載一支程式，變成 6 個）→ 真的量請求數 | 4 條紅：test_at_most_five_more_requests_after_the_first_screen、test_first_screen_requests_are_exactly_the_static_files_and_one_file_per_asset、test_server_saw_the_same_requests_in_the_same_order、test_first_screen_is_untouched |
  | R1 風險的門檻改壞（兩成改成五成）→ 白話層 | 2 條紅：test_icon_always_has_its_word、test_three_states_follow_the_rules |
  | R2 同上 → 面向的離線測試 | 1 條紅：test_risk_compares_one_year_to_five_years |
  | R3 成本的門檻改壞（一成改成五成）→ 面向的離線測試 | 2 條紅：test_bar_cost_waits_for_twenty_days、test_cost_compares_spreads_to_their_own_median |
  | R4 人民幣存摺拿到台幣那一本的價差 | 1 條紅：test_cost_compares_spreads_to_their_own_median |
  | R5 條塊的成本看錯列（不是 1 公斤那一列） | 1 條紅：test_bar_cost_waits_for_twenty_days |
  | R6 匯率卡的「5 年位置（見換匯助手）」那一行不見了 → 面向的離線測試 | 1 條紅：test_fx_cards_keep_the_lamp_and_link_to_the_helper |
  | R7 同上 → 真的開儀表板量的那一組 | 1 條紅：test_not_applicable_is_never_drawn_on_a_card_and_fx_cards_link_to_the_helper |
  | R8 分析檔讀不到被寫成資料不足 → 面向的離線測試 | 5 條紅：test_cost_compares_spreads_to_their_own_median、test_not_applicable_is_not_insufficient、test_premium_uses_own_quartiles_and_the_official_row、test_risk_compares_one_year_to_five_years、test_unreadable_files_say_so |
  | R9 同上 → 真的開儀表板、把分析檔擋掉的那一組 | 1 條紅：test_chips_say_temporarily_unavailable |
  | R10 分析檔讀不到時，「分析」摺疊區說成「沒有分析項目」 | 1 條紅：test_fold_says_so_too |
  | R11 圖示放進標題列裡面（按鈕裡有按鈕） | 3 條紅：test_clicking_a_chip_opens_only_its_own_detail、test_every_card_has_a_strip_between_head_and_body、test_metric_titles_become_clickable_terms_with_the_same_words |
  | R12 指標格標題的字被改掉 | 1 條紅：test_metric_titles_become_clickable_terms_with_the_same_words |
  | R13 「累積中 n/20」不印了 | 3 條紅：test_bar_cost_waits_for_twenty_days、test_every_chip_has_icon_name_and_word、test_premium_uses_own_quartiles_and_the_official_row |
  | R14 總覽表排序把「資料不足」排到最前面 | 1 條紅：test_overview_sorting |
  | R15 總覽表同狀態不照名稱 | 1 條紅：test_overview_sorting |
  | R16 總覽表的預設順序不照儀表板的分組 | 2 條紅：test_overview_rows_in_dashboard_order、test_overview_sorting |
  | R17 「回到預設順序」按了沒有回去 | 1 條紅：test_overview_sorting |
  | R18 點表頭裡的名詞會往外傳（名詞按鈕不擋） | 1 條紅：test_glossary_upgrades_placeholders |
  | R19 點表頭展開的解釋會觸發排序 | 1 條紅：test_table_headers_have_terms |
  | R20 名詞解釋少了規格點名的詞（視窗重疊） | 1 條紅：test_glossary |
  | R21 白話把方向寫反（1 年的起伏比 5 年大／小） | 1 條紅：test_every_template_has_three_kinds_of_cases |
  | R22 相關矩陣下面沒有「每個標的一句」 | 1 條紅：test_correlation_one_sentence_per_asset |
  | R23 試算結果沒有圖示列 | 1 條紅：test_adhoc_glance_row |
  | R24 新檔沒有被守門掃到（儀表板程式不在掃描清單裡） | 1 條紅：test_the_dashboard_script_is_inside_the_scan |
  | R25 最上面那一行分析狀態在有錯時不紅 | 1 條紅：test_overview_status_line |

- 截圖 22 張（桌機 1200、手機 390）：收合的儀表板、總覽表（預設、排序後展開一列、手機往右捲）、三種卡展開（有長歷史的 00646、黃金存摺台幣、黃金存摺人民幣）、名詞解釋展開（指標格、總覽表）、匯率卡的位置細節、條塊卡、分析分頁的風險表／成本區／相關。
- 對外請求 0（測試與量測全部打 127.0.0.1）；台銀 0；排程與 workflow 一字未改；隱私掃描（通用樣式、禁用鍵名、兩份設定檔的鍵名、加鹽 HMAC 的具名字串）與擋字串綠。

**怎麼退回**

```bash
git log --oneline stopA1-7 -1                     # 看標籤指到哪個 commit
# 合併前：分支還沒進 main，不必退；不要了就不合併
git revert -m 1 54dd3f9 && git push                # 合併後：退整個 A1-7（含驗收後的修正）
```

退回後：卡片的圖示列、指標格的名詞、分析分頁的總覽表與各區的白話都消失，儀表板回到「展開分析才抓 risk／cost」；`cost.json` 的折溢價摘要少掉 `p25Pct`／`p75Pct`（目前本來就還沒有）。資料檔不受影響。

### 驗收後的修正：六件裁決（2026-10-01；tag `stopA1-7` 之後的 commit，同一個合併；tag 不動）

**裁決與處理**

| # | 上面「六件要請你過目的事」 | 裁決 | 處理 |
|---|---|---|---|
| 1 | `analyze.py` 多輸出折溢價的第 25／75 百分位 | 接受 | 不必改 |
| 2 | 折溢價的狀態以「確定」口徑為主、預估並列在細節 | 批准（同時是 A1-4 的口徑裁決） | 不必改 |
| 3 | 摺疊區的小字改成真的 | 批准 | 不必改 |
| 4 | `js/app.js` 那一個價格名稱只在那個檔豁免 | **不要豁免，改字**——例外清單不要開始累積 | 條塊卡那句說明裡的價格名稱，改成台銀牌價欄位的正式名稱（「本行…」那一欄，本來就在例外清單裡）；守門拿掉為這個檔開的例外 |
| 5 | 自己定的四條 | 批准，補一條：成本「不適用」的標的圖示不畫，但展開的細節要列出靜態費用表裡它的項目——不適用是沒有動態成本，不是沒有成本 | 總覽表展開的細節多一段「固定的費用」 |
| 6 | S&P 500 指數「最常同方向」的是它自己的總報酬版本 | 兩者互相排除在「最同向」候選之外，取下一個；文件註明、加測試與突變 | 卡片摺疊區與分析分頁「每個標的一句」都取下一個 |

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `js/app.js` | 條塊卡「比存摺貴」那句說明、歷史還不夠時圖表下面那句說明，與同一段的一行註解，共三處價格名稱改成牌價欄位的正式名稱。數字、算法、版面都沒有變 |
| `scripts/test_analysis_guards.py` | 拿掉只給 `js/app.js` 的那一個例外與擋字串函式的「額外例外」參數（現在每個檔案用同一份例外清單）；多一條測試：舊的寫法照擋、擋字串函式不收任何額外例外、例外清單維持 9 個 |
| `js/card-analysis.js` | ① `SAME_THING`（目前一組：`gspc`／`sp500tr`）：挑「最同向」時把同一個東西的另一個版本排除，取下一個；「最不相關」與「反向最強」不受影響。② 成本「不適用」的面向多帶 `facts`：靜態費用表裡這個標的的項目（名稱、數值、出處標籤、查核日期）；是一個百分比的才有白話行，分級費率之類的只列原文、不硬套白話。細節多一個小標「固定的費用（不隨時間變動，所以沒有狀態）」 |
| `js/analysis.js` | 總覽表算面向時把靜態費用表交進去（分析分頁本來就讀這個檔，不多請求）。儀表板的卡片不載靜態費用表（首屏之後仍然只多 5 個請求），所以這一段只出現在總覽表展開的細節 |
| `css/style.css` | 只新增 `.asp-facts-title` 一條 |
| `scripts/test_card_analysis.html`／`_js.py`、`scripts/test_aspects.html`／`_js.py`、`scripts/test_analysis_page.html`／`_js.py` | 卡片分析 9 → 10 題（同一個指數的兩個版本不互相當最同向）；面向 15 → 16 題（不適用仍列出固定費用：有項目的列出、沒有項目的不印小標、分級費率不印白話行）；分析分頁 38 → 39 題（總覽表不適用那一列的細節、相關句的雙胞胎） |
| `docs/ANALYSIS.md` | 5.9 註明 S&P 500 與總報酬版本不互相當「最同向」；5.11「不適用」補一段：不是沒有成本、細節列靜態費用、卡片為什麼看不到這一段 |
| 各 HTML | `bump_assets.py` 版本 20261001-1 |

沒有動的：`scripts/analyze.py`、`.github/workflows/`、`data/schedule.json`、`scripts/fetch_data.py`、`scripts/report.py`、`js/indicators.js`、任何資料檔。對外請求 0；台銀 0。

**怎麼驗的**

- 單元測試 **540 條全綠**（537 → 540：上表三組各多一題；守門多一條）。A1-5 的燈號 fixture 仍綠。
- 首屏前後的請求數重量：首屏 23、之後 5、點開一張卡再展開「分析」後總數 28，跟 tag 那一版一樣。
- **改前改後比對重做**（改前仍是分支起點 24c737e）。DOM 逐行：合計少了 99 行、多了 5145 行（tag 那一版是 97／5134；多出來的是條塊卡改字的那兩行與總覽表多的固定費用）。文字層：

  ```
  合計：改前 2596 段字，改後少了 11 段
    9 段：每張展開的卡各一次的「風險、相關、成本；展開才讀取」（裁決 3 批准的改字）
    2 段：條塊卡「比存摺貴」那句說明，儀表板與條塊卡展開各一次（裁決 4 的改字；同一句換了價格名稱，數字沒變）
  ```

  除了這兩句改字，改前的每一段字改後都還在。
- **突變對照組 81 組全部符合預期**（上面 73 組重跑仍然全部符合，基準變成 540 條綠；這一段新增 8 組）：

  | 改壞的方式 | 結果 |
  |---|---|
  | V1 S&P 500 與它的總報酬版本又互相當「最同向」（互相排除被拿掉）→ 卡片摺疊區 | 1 條紅：test_same_index_in_two_versions_is_not_its_own_most_aligned |
  | V2 同上 → 分析分頁「每個標的一句」 | 1 條紅：test_correlation_one_sentence_per_asset |
  | V3 不適用的細節不列固定費用 → 面向的離線測試 | 1 條紅：test_not_applicable_still_lists_the_fixed_costs |
  | V4 同上 → 總覽表展開的那一列 | 1 條紅：test_overview_not_applicable_row_lists_fixed_costs |
  | V5 總覽表沒有把靜態費用表交給面向的判定 | 1 條紅：test_overview_not_applicable_row_lists_fixed_costs |
  | V6 分級費率（不是一個數字）下面被印成「資料不足」 | 1 條紅：test_not_applicable_still_lists_the_fixed_costs |
  | V7 `js/app.js` 那個價格的名字改回舊的寫法 → 守門的擋字串（沒有例外） | 1 條紅：test_no_judgement_words_in_analysis_files |
  | V8 守門又開了一個例外（把舊的寫法加進例外清單） | 1 條紅：test_no_file_has_an_exemption_of_its_own |

- 截圖 23 張（多一張：總覽表展開台積電那一列——成本寫「不適用」、細節列出證券交易稅等固定費用）；其餘 22 張用修正後的程式重拍。
- 排程與 workflow 一字未改；隱私掃描與擋字串綠。

**怎麼退回**

```bash
git revert --no-edit stopA1-7..feat/stopA1-7       # 合併前：只退這一段修正，回到 tag 那一版
git revert -m 1 54dd3f9 && git push                 # 合併後：修正跟 A1-7 在同一個合併裡，要退就整個 A1-7 一起退
```

---

## 2026-09-29 · 分析系列 A1-6 人民幣補齊＋「52 週」名符其實＋換匯助手＋「一眼看懂」（標籤 `stopA1-6`；2026-09-30 合併 c3c24ba）

**合併紀錄（2026-09-30）**

- 合併 commit **c3c24ba**（`--no-ff`；第一個 parent 是 main 的 95b4f92，第二個是分支的 55e24d6）。合併在另一個暫時的 worktree 做，主目錄留在 main、排程照跑。
- 衝突只有四個分析輸出檔（`data/analysis` 的 status／risk／decompose／cost）：main 上的是舊程式每天 15:30 算的，分支上的是 9/27 的種子。兩邊都不拿——改在沙盒用「合併後的程式＋main 最新的資料」真的跑一次 `analyze.py --slot review`，拿它的產出當新種子：結束碼 0、對外請求 3 次（人民幣週線補最近幾週、美元兌人民幣週線補最近幾週、證交所那一檔）；其餘 11 條長歷史週一已經補過、這次不必抓。
- 匯率日線兩個檔沒有衝突：9/25 中秋節、9/28 教師節補假，台銀沒有掛牌，main 從分支開出去之後沒有新的匯率點；合併後各 400 點（2025-02-13～2026-09-24），日期由舊到新、沒有重複。
- 合併後的樹上全套測試 481 條全綠。

**先講四件跟原本預期不一樣的事**

1. **歷史模擬的主要比較比不出差別。** 裁決定的主要比較是「B 加期限保底、期限＝視窗結束、總預算跟 A 一樣」。總預算＝月預算 × 月數的時候，保底（剩餘 ÷ 剩餘月數）每個月都剛好等於月預算，規則表給的比例最高也只是 100%，所以 B 每個月換的跟 A 一模一樣——76 個視窗全部平手，兩個門檻當然都沒過，裁決是「固定分批」。這是算術上必然的結果，不是資料的結論。另外算了兩組對照（所需步調是月預算的 75%、50%，規則才有挪動的空間）：B 換得比較便宜的視窗有 71.1%、73.7%，但中位數改善只有 0.104%、0.338%，都不到 0.5% 的門檻——所以不管用哪一個口徑，結論都是固定分批。對照只列出來，不進裁決。**（驗收裁決，2026-09-29：錯在比較的定義，不在資料——改成「預算池」模型重跑，見本節最後的「驗收後的修正」。下面「歷史模擬的結果」那張表與這兩組對照是舊模型的，留作紀錄。）**
2. **匯率兩張卡改前寫的不是「52 週」，是「近 7 個月」；條塊卡根本沒有區間位置那一塊。** 舊程式在資料不滿一年時寫「近 N 個月」（天數 ÷ 21 四捨五入），沒有謊稱 52 週。條塊卡展開只有走勢圖與一句「本站自 2026-08-31 起累積…共 12 個交易日」，沒有標題可以改，改前改後的 DOM 一行都沒變。
3. **匯率日線回補之後，兩張匯率卡的視窗從 145 天變成完整的 252 天，百分位跟著變；人民幣卡的燈號從「中性」變成「相對高檔區」。** 這是資料變完整造成的（人民幣一年內的低點 4.252 在 2025-09，舊的 145 天視窗看不到），燈號的算法一個字沒改（A1-5 釘住的 fixture 仍綠）。美元卡第 38 → 第 72 百分位，仍是「中性」。（驗收裁決：接受——燈號變化來自資料補齊，算法未改。）
4. **「一眼看懂」有兩個門檻是這一輪自己定的**（裁決只給了狀態詞，沒給數字）：成本相對自己一年的中位數 ±一成、風險的 1 年波動相對 5 年波動 ±兩成。位置的 40／60 是裁決給的。規則句永遠印在圖示旁邊，要改只改 `js/plain.js` 一個地方。（驗收裁決：兩個門檻與保底月初結算都批准。）

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/probe_analysis_sources.py`、`scripts/test_probe_analysis.py` | 探測多一組 `fx`：F-10（人民幣 2006 年起整段）、F-11a／F-11b（美元、人民幣最近約 400 個營業日）、Y-17（美元兌人民幣週線 `CNY=X`）。`probe-analysis.yml` 一字未改 |
| `scripts/analyze.py` | 週線長歷史的對象從「美元」改成「所有經 FinMind 取得的匯率」，人民幣 −1 的缺值列剔除並在檔頭記筆數；多一條只給拆解用的序列 `usdcny`（不進風險表與相關矩陣）；`cost.json` 多 `goldSpreadCny`（人民幣存摺的價差，跟台幣那張同一個算法）；`decompose.json` 多 `cny`（人民幣對台幣的變動拆成兩塊）；新的 `data/analysis/fx.json`：百分位、距 1 年低點高點、即期與現鈔的價差、分批規則表、歷史模擬與裁決。歷史模擬只在週一重算 |
| `scripts/publish.py`、`scripts/test_publish.py` | 雲端的擁有清單加 `data/analysis/fx.json` 與 `data/history-long/usdcny.json`（人民幣的長歷史是雲端資產、自動列入） |
| `scripts/backfill_fx_history.py`、`scripts/test_backfill_fx.py`（新） | 匯率日線一次性回補：只增不改、重疊區逐日比對、任何一筆差超過 0.3% 就停下來不寫檔、新補的點標 `dateSource=finmind`、補完只留最近 400 點 |
| `data/history/fx_usd.json`、`data/history/fx_cny.json` | 來源各有 262 個更早的日期（2025-02-04 起）；檔案上限 400 點，所以各留下最近的 255 個（2025-02-13～2026-02-26）。原本的 145 點一個欄位都沒動 |
| `data/history-long/fx_cny.json`、`data/history-long/usdcny.json`（新）、`data/analysis/*.json` | 種子：2026-09-27 23:26 在 worktree 的沙盒副本實跑產生（3 個對外請求）；`fx.json` 在 2026-09-29 用最終版程式離線重算一次（0 個請求，數字與 9/27 完全相同，只有回測的兩句說明改成白話） |
| `js/indicators.js` | 多匯出 `FULL_YEAR_DAYS`（227）與四個只管文字的函式 `windowStart`／`windowName`／`windowSince`／`windowLabel`／`signalRule`；`rangePosition` 的 `full` 改用同一個常數（結果相同）。`positionSignal`、`computeAll` 的輸出一個字沒變 |
| `js/app.js` | 區間位置的標題、規則句、燈號的提示文字改成向 `js/indicators.js` 要名稱（自己不再判斷）；要抓 `cost.json` 的卡加人民幣存摺 |
| `js/card-analysis.js` | 人民幣存摺卡顯示自己的存摺價差；人民幣匯率卡多一個「換匯助手 →」入口（連到 `analysis.html#fx`） |
| `js/plain.js`（新） | 三態狀態（位置、成本、風險）、圖示、規則句、白話模板；資料不足一律輸出「資料不足」。純函式 |
| `js/glossary.js`（新） | 11 個名詞的一句話定義；點一下展開、再點一下收起 |
| `js/fxplan.js`（新） | 私人設定的讀寫格式、驗證（每一筆已換紀錄的匯率必填）、本月試算（兩種做法都算、保底、取較大者、本月已換的扣一次）。設定檔的鍵名只在這一檔 |
| `js/analysis.js`、`analysis.html` | 新的「換匯助手」區（放在分析狀態的下面）：一眼看懂三格 → 位置 → 換匯成本 → 規則表 → 歷史模擬 → 我的換匯設定與本月試算 → 固定註記兩句；拆解多人民幣那一段、成本多人民幣存摺的價差；開頁多讀一個 `fx.json`；從卡片帶 `#fx` 進來會捲到換匯助手 |
| `css/style.css` | 只新增 `.glance*`、`.plain-line`、`button.term`、`.term-def`、`.ana-linkrow`、`.ana-link`，不改既有選擇器 |
| `scripts/test_plain.html`／`test_plain_js.py`、`scripts/test_fxplan.html`／`test_fxplan_js.py`（新） | 白話層 7 題＋接線 4 條；換匯設定 7 題 |
| `scripts/test_analysis_page.html`／`_js.py`、`scripts/test_card_analysis.html`／`_js.py`、`scripts/test_indicators.html`／`_js.py` | 分析分頁 18 → 28 題（換匯助手 10 題）＋接線 1 條；卡片 7 → 9 題；指標多 7 條視窗名稱的測試（A1-5 的 30 組 fixture 原封不動） |
| `scripts/test_analysis_guards.py` | 掃描清單加 10 個新檔；擋字串多三個詞（連同原有的一個，「呈現原則」裡那一句四個詞的列舉是唯一的豁免）；隱私樣式多「預算／已換／目標總額／目標金額後面直接接數字」；資料檔禁用鍵名多 7 個；換匯設定檔的鍵名只准在 `js/fxplan.js`；離線產出的 `fx.json` 也掃 |
| `docs/ANALYSIS.md`、`README.md` | 5.1／5.2／5.3／5.5／5.9 補人民幣；新增 5.10 匯率與換匯助手、第 6 節「呈現原則」；「下一步」改成第 7 節並記 A1-7 的規格 |
| 各 HTML | `bump_assets.py` 版本 20260929-1 |

**怎麼驗的**

- 單元測試 **472 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`；頁面測試用 Chrome）。A1-5 的燈號 fixture 仍綠。
- **探測**（run 36328892056，2026-09-27 23:15，4 個請求，全部可用）：

  | 列 | 內容 | 結果 |
  |---|---|---|
  | F-10 | FinMind 人民幣 2006 年起 | 5131 列，其中 1738 列是 −1（缺值）；有效資料 2013-01-02～2026-09-24 |
  | F-11a | FinMind 美元最近約 400 個營業日 | 407 列，自 2025-02-04 |
  | F-11b | FinMind 人民幣最近約 400 個營業日 | 407 列，自 2025-02-04 |
  | Y-17 | Yahoo `CNY=X` 週線 | 2001-06-25～2026-09-27，1289 點，幣別 CNY，30 個空值 |

- **日線回補**（2026-09-27，2 個請求）：

  | 幣別 | 重疊的日子 | 完全一致 | 不一致 | 超過 0.3% | 來源有的更早日期 | 補完（上限 400 點） |
  |---|---|---|---|---|---|---|
  | 美元 | 145 天 | 145 天 | 0 筆 | 0 筆 | 262 個（2025-02-04～2026-02-26） | 400 點（2025-02-13～2026-09-24），其中新補 255 點 |
  | 人民幣 | 145 天 | 145 天 | 0 筆 | 0 筆 | 262 個（2025-02-04～2026-02-26） | 400 點（2025-02-13～2026-09-24），其中新補 255 點 |

- **沙盒實跑**（worktree 的副本裡跑 `analyze.py --slot review`，真倉庫不碰）：3 個對外請求（人民幣週線、美元兌人民幣週線各整段一次，外加淨值那一個），結束碼 0；風險矩陣 12 個序列（沒有 `usdcny`）。
- **歷史模擬的結果（舊模型，已被「驗收後的修正」取代，留作紀錄）**（人民幣，36 個月的視窗 76 個：2017-07～2023-10 起算；決策點 111 個）：

  | 比較 | B 換得比較便宜的視窗 | 平手 | 中位數改善 | 最差 | 最好 | 純 B 沒換完預算的視窗 | 進裁決？ |
  |---|---|---|---|---|---|---|---|
  | 主要比較：總預算＝月預算 × 36 個月 | 0.0% | 100.0% | 0.000% | 0.000% | 0.000% | 100.0% | 是 |
  | 對照：所需步調是月預算的 75% | 71.1% | 11.8% | +0.104% | −0.677% | +0.511% | 63.2% | 否 |
  | 對照：所需步調是月預算的 50% | 73.7% | 0.0% | +0.338% | −2.045% | +1.693% | 39.5% | 否 |

  裁決：**預設顯示固定分批**（門檻：B 換得比較便宜的視窗 ≥ 55% 而且中位數改善 ≥ 0.5%，只看主要比較）。純 B（不保底）一般只花掉 58.0% 的預算，它的平均匯率中位數 4.37983、同一批視窗 A 是 4.41583——花的錢比較少，不能直接比。
  **這 76 個視窗逐月往後移、彼此大量重疊，不是獨立樣本；勝率的參考價值要打折。**
- **改前改後 DOM 比對**（同一個假「現在」；版本號、隨機的漸層 id、快取參數正規化之後，一個標籤一行）：
  - 只換程式、資料不動：儀表板 706 行裡 2 行改字（兩張匯率卡燈號的提示文字）；展開的卡——條塊、人民幣存摺、台幣存摺、00646 都是 0 行不同；美元卡 3 行改字；人民幣卡 3 行改字＋6 行新增（「換匯助手 →」）。分析分頁 1 行改字（網頁描述）＋40 行新增。沒有任何一行是單純被拿掉的。
  - 程式＋資料都換：另外多出資料造成的差異（匯率卡 145 天 → 400 天、百分位與最低價、人民幣卡的燈號；人民幣卡與人民幣存摺卡的分析區從「這個標的目前沒有分析項目」變成實際的列）。
- **突變對照組 48 組全部符合預期**（1 組基準綠、47 組紅；改壞 → 只跑指定的測試 → 必須紅 → 還原）：

  | 改壞的方式 | 結果 |
  |---|---|
  | M1 回測偷看未來（百分位的視窗往後多拿 52 根） | 1 條紅 |
  | M2 規則表對應改壞（< 20% 給 60%） | 1 條紅 |
  | M3a／M3b 期限保底拿掉（回測；瀏覽器的本月額度） | 5 條紅；3 條紅 |
  | M4a／M4b 現鈔與即期欄位對調（fx.json；頁面上兩欄放反） | 1 條紅；1 條紅 |
  | M5a～M5d 月預算或已換金額出現在公開輸出、DOM 屬性、表單 id、公開的測試檔 | 2 條紅；1 條紅；3 條紅；1 條紅 |
  | M6a～M6d 資料不到門檻仍顯示「52 週」（門檻改 10；標題寫死；名稱寫死；fx.json 的門檻改 10） | 5 條紅；1 條紅；2 條紅；3 條紅 |
  | M7a～M7c 價差公式改壞（人民幣存摺；卡片拿錯那一張；匯率） | 3 條紅；2 條紅；1 條紅 |
  | M8 既有燈號邏輯被改（門檻 25 改 30） | 1 條紅（A1-5 的 fixture） |
  | P1 圖示沒有並列狀態詞 | 3 條紅 |
  | P2 圖示沒有印規則 | 2 條紅 |
  | P3a～P3c 出現那四個詞（狀態詞；文件換一種寫法；頁面措辭） | 7 條紅；1 條紅；3 條紅 |
  | P4 白話模板遇到資料不足輸出空字串 | 4 條紅 |
  | P5 顯示面向計數 | 1 條紅 |
  | P6 位置的門檻跟印出來的規則不一致 | 2 條紅 |
  | B1～B3 回補把重疊的日子當新增、拿掉 0.3% 檢查、覆寫既有的點 | 2 條紅；1 條紅；1 條紅 |
  | F1～F4 匯率不填也放行、本月已換扣兩次、B 的比例乘錯、設定檔多一個日期戳欄位 | 1 條紅；1 條紅；3 條紅；2 條紅 |
  | D1～D4 裁決門檻改鬆、視窗重疊那一句拿掉（fx.json／頁面）、每天都重算、沒過門檻仍顯示 B | 1 條紅；1 條紅／1 條紅；1 條紅；1 條紅 |
  | C1 「換匯助手 →」入口拿掉 | 1 條紅 |
  | G1／G2 存檔的 commit 訊息帶時間戳、金額印到主控台 | 1 條紅；1 條紅 |
  | K1／K2 名詞解釋少一個詞、儀表板首屏多載白話層 | 1 條紅；2 條紅 |
  | L1 週線長歷史只抓美元 | 3 條紅 |
  | X1／X2 人民幣拆解公式改壞 | 1 條紅；1 條紅 |
  | U1 fx.json 沒列進雲端的擁有清單 | 1 條紅 |

- 儀表板首屏維持 7 個靜態檔（白話層與名詞解釋這一輪只掛在分析分頁）；分析分頁多載 3 支 JS、多讀 1 個 JSON。
- 台銀請求 0 次；排程、workflow 的觸發與時刻一字未改；隱私掃描與擋字串綠；公開倉庫裡沒有任何真實的預算、目標、已換金額或期限。

**怎麼退回**

```bash
git revert --no-edit e3d2c5c..stopA1-6          # 合併前：在分支上把 A1-6 的全部 commit 反轉
git revert -m 1 c3c24ba && git push              # 合併後：退整個 A1-6（2026-09-30 合併）
```
退回後換匯助手、人民幣的分析項目與「近 n 個交易日」的寫法消失，卡片回到「近 N 個月」；私人倉庫裡的 `fx-plan.json` 不受影響（公開倉庫從來沒有它）。
匯率日線那兩個檔會回到 145 點——退回之後雲端每天照常往後追加，不會壞。

### 驗收後的修正：回測改成「預算池」模型（tag `stopA1-6` 之後的 commit，同一個合併；tag 不動）

**為什麼改**：第一版的主要比較是「規則比例 × 月預算、保底＝剩餘 ÷ 剩餘月數」。總預算＝月預算 × 月數時保底每個月都等於月預算，而規則比例最高也只有 100%，B 永遠只能等於 A——這個比較沒有意義。驗收裁決改成預算池：沒換的錢累積起來，便宜時一次多換。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/analyze.py` | 新的 `fx_pool_quota`（本月額度＝max(規則比例 × 池子, 保底)，不超過池子）與 `fx_pool_plan`（每個月先把月預算放進池子，再換掉本月額度）；`fx_backtest` 的 B 改成「預算池＋期限保底（池子 ÷ 剩餘月數，期限＝視窗結束）」，純 B 改成「規則比例 × 池子、不保底」；結果多「B 在期限前換完的視窗比例」與 A／B 平均匯率的中位數；拿掉舊模型的兩組對照；結果裡記模型名稱，沿用上一次的結果之前先核對，模型不同就重算 |
| `js/fxplan.js` | 本月試算改用預算池：池子＝從計畫起始月累積的預算 − 這段期間已換的台幣；規則額度＝比例 × 池子；保底＝池子 ÷ 剩餘月數，有目標總額時再跟目標那一半比取較大者；本月額度不超過池子。私人設定檔多一個欄位（計畫起始月，必填）。有期限就有保底（不再要求同時有目標） |
| `js/analysis.js` | 本月試算的表改成：預算池（月初）、固定分批、規則額度、保底、本月額度、本月已換、本月還沒換的額度、預算池（現在）；表單多一格計畫起始月（新的設定檔先帶這個月）；回測表只剩主要比較一列，純 B 另列一段；規則表寫明比例是對預算池算的 |
| `js/plain.js`、`js/glossary.js` | 白話那一句改成「規則額度是預算池的 X%」；名詞解釋多「預算池」 |
| `scripts/test_analyze.py`、`scripts/test_fxplan.html`／`_js.py`、`scripts/test_analysis_page.html`、`scripts/test_plain.html` | 回測與本月試算的測試全部改成預算池的版本（見下面的對照組）；換匯設定 7 → 9 題 |
| `scripts/test_analysis_guards.py` | 設定檔鍵名清單與資料檔禁用鍵名加新欄位；隱私樣式加「預算池／池子後面直接接數字」 |
| `data/analysis/fx.json` | 種子用新模型離線重算一次（0 個對外請求）；幣別那一段（位置、成本、規則表那一檔）與重算前完全相同 |
| `scripts/analyze.py`（第二次驗收回覆） | `fx_big_wins`：改善達到裁決門檻的視窗，各自找出單月換最多的那個決策日再按日期歸戶，結果寫在回測結果的 `bigWins`；回測結果多一個格式版本，沿用上一次的結果之前模型與格式都要對得上 |
| `js/plain.js`（第二次驗收回覆） | 新的白話模板：歷史模擬的結論那一句——勝率、中位數、跟換一次的即期價差比、贏得多的那幾次是不是同一次事件、所以預設哪一種；完全由數字決定，事件不只一次時照實說分散在幾個月份 |
| `js/analysis.js`（第二次驗收回覆） | 歷史模擬區在裁決的下面印那一句結論，並列出贏得多的視窗與它們大筆換匯的那一天 |
| `docs/ANALYSIS.md`、`README.md` | 5.10 改寫成預算池模型，並記下舊模型為什麼比不出差別；補「贏得多的視窗來自哪裡」 |

**一個沒有照字面做的地方（請驗收時確認）**：裁決寫「本月已換自然從池子扣掉，不再需要另外扣一次」。照字面做的話，規則額度＝比例 ×「扣掉本月已換之後的池子」——換完本月額度再回來看頁面，它會叫人把剩下的池子再乘一次比例，一直換下去。所以本月額度改用「月初的池子」算（這個月的預算已放進去、這個月換的還沒扣），這個月已經換的再從額度裡扣一次；頁面同時印出「本月還沒換的額度」與「現在的池子」。這跟回測的做法一致（每個月一個決策點、用換之前的池子算），也跟已批准的「保底月初結算」一致。

**第二次驗收回覆（2026-09-30）**：新的回測摘要表通過，裁決「固定分批」照實顯示。「月初的池子」的做法接受、不照字面；頁面同時印「本月還沒換的額度」與「現在的池子」。三個細節全部批准——固定分批時本月額度＝max(月預算, 保底) 且不超過池子；計畫起始月以前的紀錄不扣池子、但算進已換人民幣並提醒；舊模型的兩組對照拿掉；計畫起始月必填、有期限就有保底。另外要求把「改善大的視窗全部押在同一天」寫進頁面的歷史模擬區（見下表最後三列）。

**怎麼驗的**

- 單元測試 **481 條全綠**。A1-5 的燈號 fixture 仍綠。
- **新模型的結果**（人民幣，36 個月的視窗 76 個：2017-07～2023-10 起算；決策點 111 個，2017-07-07～2026-09-04；2026-09-29 離線重算）：

  | 比較 | B 換得比較便宜的視窗 | 平手 | 中位數改善 | 最差／最好 | B 在期限前換完的視窗 | 平均匯率的中位數（A／B） |
  |---|---|---|---|---|---|---|
  | 主要比較：B（預算池＋期限保底）對 A（每月固定換一個月預算）；總額一樣 | 89.5% | 0.0% | +0.089% | −0.063%／+1.244% | 100.0% | 4.41583／4.39674 |

  純 B（不保底）：沒換完預算的視窗 77.6%，一般只花掉 95.9% 的預算；它的平均匯率中位數 4.39119，同一批視窗 A 是 4.41583——花的錢比較少，不能直接比。
  **裁決：預設顯示固定分批。** 兩個門檻只過了一個：B 換得比較便宜的視窗 89.5%（≥ 55%，過），中位數改善 0.089%（≥ 0.5%，沒過）。
  **這 76 個視窗逐月往後移、彼此大量重疊，不是獨立樣本；勝率的參考價值要打折。** 具體來說：改善 ≥ 0.5% 的視窗有 16 個，全部是 2022-07 以後起算的，而且它們的大額換匯都落在同一天——2025-05-02（人民幣兌台幣一個月內從 4.570 掉到 4.276）。也就是同一次事件被算了 16 次。其餘 60 個視窗的改善在 −0.063%～+0.5% 之間；8 個變差的都是 2019 年起算的。
  頁面的歷史模擬區用一句白話講這件事（由程式照數字產生，不是寫死的）：「歷史上依位置分批在大多數期間（89.5%）略勝，但中位數只多 0.09%，比換一次的價差（1.06%）還小；贏得多的那 16 個期間（便宜 0.5% 以上），大筆換匯都落在同一次事件（2025-05），等於同一件事被重複算了 16 次。所以預設顯示固定分批。」
- **突變對照組重跑 67 組全部符合預期**（1 組基準綠、其餘改壞都紅）。跟回測模型、本月試算、歷史模擬那一句有關的：

  | 改壞的方式 | 結果 |
  |---|---|
  | M1 回測偷看未來（百分位的視窗往後多拿 52 根） | 2 條紅 |
  | D1 裁決門檻改鬆（中位數改善 0.5% 改 0.1%） | 2 條紅 |
  | M3a 期限保底拿掉（回測裡的 B 不保底） | 5 條紅 |
  | M3b 期限保底拿掉（瀏覽器的本月額度只看預設做法） | 5 條紅 |
  | M9a 本月額度超過池子（瀏覽器：把上限拿掉） | 2 條紅 |
  | M9b 本月額度超過池子（回測：把上限拿掉） | 1 條紅 |
  | M10a 規則比例乘月預算而不是乘池子（回測） | 3 條紅 |
  | M10b 規則比例乘月預算而不是乘池子（瀏覽器） | 5 條紅 |
  | M11a 池子不累積（回測：每個月只有一個月預算） | 4 條紅 |
  | M11b 池子不累積（瀏覽器：永遠只有一個月預算） | 5 條紅 |
  | M12 換完本月額度之後，用剩下的池子再乘一次比例 | 1 條紅 |
  | M13 計畫開始以前換的也從池子扣 | 1 條紅 |
  | F2 目標那一半的保底不以月初為準（本月已換的被扣兩次） | 1 條紅 |
  | F3 B 的比例乘錯（永遠用 100%） | 4 條紅 |
  | F4 設定檔多一個日期戳欄位（asOf） | 2 條紅 |
  | F5 計畫起始月不填也放行 | 1 條紅 |
  | F6 表單欄位用新鍵名當 id（計畫起始月） | 3 條紅 |
  | D3 回測每天都重算（只在週一重算的規則拿掉） | 2 條紅 |
  | D5 不核對模型就沿用上一次的回測結果 | 1 條紅 |
  | D6 純 B 的沒換完比例不顯示在頁面上 | 1 條紅 |
  | S1 不管資料、一律說是同一次事件 | 2 條紅 |
  | S2 「贏得多」的門檻用錯（用 0 當門檻） | 1 條紅 |
  | S3 歸戶時不看日期（全部算成第一個視窗的那一天） | 1 條紅 |
  | S4 單月換最多的那一天取錯（永遠取第一個月） | 1 條紅 |
  | S5 頁面不印歷史模擬的那一句結論 | 1 條紅 |
  | S6 中位數跟一次價差比的方向寫反 | 1 條紅 |
  | D7 格式版本不核對就沿用（舊的快取沒有「贏得多的視窗來自哪裡」） | 1 條紅 |

- 對外請求 0；排程與 workflow 一字未改；隱私掃描與擋字串綠。

**怎麼退回**

```bash
git revert --no-edit stopA1-6..feat/stopA1-6      # 合併前：只退這一段修正，回到 tag 那一版（舊模型）
git revert -m 1 c3c24ba && git push              # 合併後：修正跟 A1-6 在同一個合併裡，要退就整個 A1-6 一起退
```

---

## 2026-09-26 · 分析系列 A1-5 儀表板整合（第一版）（標籤 `stopA1-5`；等驗收後合併）

**先講一件出事的：** A1-3 的文件腳本把 `docs/ANALYSIS.md` 弄壞了——算第 6 節的切片時，終點找到第 2 節那句頁尾，切片變成空字串，`str.replace("", 新段落)` 把新段落塞進每一個字元之間，整份公開方法文件變成 24 MB（120,955 行），並隨 A1-3 的合併（6292af2）上線；A1-3 的測試沒抓到（守門只掃判斷用語，檔案「乾淨」）。2026-09-25 晚上發現，已直接在 main 修復（commit 2520c18：從 A1-2 合併後的版本重建、用「錨點必須剛好出現一次」補回 A1-3 的段落）。教訓：切片與 replace 的錨點一律要斷言剛好出現一次，空字串當 old 會塞遍全檔。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `js/card-analysis.js`（新） | 純函式：`facts(asset, risk, cost)` 從 risk.json／cost.json 挑出這一張卡的事實；`render(facts)` 畫成幾行小表。風險：年化波動 1／5 年、最大回檔（高點、低點、回到高點的日期）、目前距高點＋小橫條（刻度 0 到該標的歷史最大回檔，顏色中性）；相關：最同向（r 最高）、最不相關（絕對值最接近 0）、反向最強（r < −0.3 才顯示）；成本有才顯示：00646 追蹤差（主口徑 3 年年化）、折溢價（預估／確定各一筆，累積不到 20 個交易日標「累積中 n/20」，滿了才顯示中位數）、存摺價差、條塊各規格溢價。每一列都有資料標籤與資料日期；橫條的週線日期與卡片上方的日線報價日期並列印出。沒有週線長歷史的卡寫「不在相關矩陣」、只列成本；什麼都沒有的卡寫「這個標的目前沒有分析項目」 |
| `js/app.js` | 卡片展開後的 body 最下面多一個預設收合的「分析」摺疊區（`<details class="ana">`）；第一次展開才抓 `data/analysis/risk.json`（需要成本項的四張卡再抓 `cost.json`），抓過存在 `state.analysis`；開頁時什麼都不抓。其餘一行不動 |
| `index.html`、`history.html`、`records.html`、`settings.html` | 導覽列加「分析」；首頁多載 `js/card-analysis.js`（首屏靜態檔 6 → 7）；首頁頁尾那個舊連結改成分析分頁 |
| `analysis.html`（新）、`js/analysis.js`（由 `analysis-debug.js` 改名） | 分析分頁：狀態列、風險總表可按欄排序（`sortableTable`／`sortTable`，資料不足與「—」永遠排最後）、相關矩陣色階（`corrColor`：負相關藍、接近 0 透明、正相關橙；格內數字才是訊息）＋圖例、成本、拆解、集中度、試算（同一套 JS 只換掛載點）；手機上風險總表與矩陣整張橫向捲動、矩陣第一欄固定 |
| `analysis-debug.html` | 改成只做跳轉的小頁（`meta refresh`＋`location.replace`，留一句「此頁已搬到分析分頁」），不載任何程式 |
| `css/style.css` | 只新增 `.ana*` 規則（摺疊區、列、標籤、橫條、手機單欄），不改既有選擇器 |
| `scripts/fixtures/indicators_signal.json`、`scripts/test_indicators.html`、`scripts/test_indicators_js.py`（新） | 燈號釘住：fixture 是在動任何前端程式**之前**用 main 的 `js/indicators.js` 對合成序列產生的（30 組、三種燈號都有），之後每次重算逐值比對 |
| `scripts/test_card_analysis.html`、`scripts/test_card_analysis_js.py`（新） | 7 題：展開有內容、每列有四種標籤之一與日期、橫條刻度與兩個日期、相關三條規則、四張沒長歷史的卡、20 天中位數規則、沒有判斷用語與加總數字 |
| `scripts/test_analysis_page.html`、`scripts/test_analysis_page_js.py`（由 debug 測試改名） | 18 題（＋矩陣格數＝標的數平方與色階、風險表排序）；wiring：分析頁腳本順序與六段、五頁導覽列都有「分析」、跳轉頁會跳、首屏靜態檔釘 7 且 `boot()`／`loadHistories()` 不碰分析檔、README／ANALYSIS.md／首頁沒有「暫時頁」字樣 |
| `scripts/test_analysis_guards.py` | 掃描清單換成新檔名並加 `analysis.html`、`js/card-analysis.js`、三個新測試檔 |
| `docs/ANALYSIS.md`、`README.md` | 5.5 補 all_etf.txt 17:00 那一句、5.9 儀表板整合、「暫時頁」字樣清掉；README 檔案結構與進度 |
| 各 HTML | `bump_assets.py` 版本 20260925-3 |
| `scripts/test_analysis_guards.py`（驗收後、tag 之後的 commit，同一個合併） | docs/ 底下每個 .md 的大小上限：ANALYSIS.md 120 KB、CHANGELOG.md 512 KB、scheduler-setup.md 32 KB、其他 128 KB（各取當時實際大小的 5 倍左右：正常改一次不會長 5 倍，接近上限要有意識地調高）；掃描器有自己的對照組。A1-3 那種 24 MB 事故以後會被自動擋下 |

**怎麼驗的**

- 單元測試 **402 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`；頁面測試用 Chrome）。既有的 freshness 19 條照舊綠。
- **首屏請求數**（本機 http 伺服器＋瀏覽器實測，不含 HTML 本身）：改前 6 個靜態檔＋16 個 JSON＝22；改後 **7＋16＝23**，多的只有 `js/card-analysis.js`；開頁沒有任何 `data/analysis/` 請求，`decompose.json` 與 `history-long` 完全不載。
- **改前改後 DOM 比對**（`--dump-dom`，同一份資料、同一個假「現在」，把 `?v=` 與迷你走勢線每次隨機的漸層 id 正規化之後）：只有 4 行不同——導覽列多一行「分析」、`<script src="js/card-analysis.js">` 多一行、首頁頁尾那一行連結改字（1 刪 1 增）；儀表板 14 張卡的 DOM 與文字完全相同。diff 全文在驗收報告。
- 突變對照組（改壞 → 只跑指定測試檔 → 必須紅 → 還原）**16 組全部符合預期**（4 組基準綠、12 組紅）：

  | 改壞的方式 | 結果 |
  |---|---|
  | M1 把資料標籤拿掉 | 1 條紅 |
  | M2 把數字換成一個假的加總數字 | 2 條紅 |
  | M3 相關矩陣少畫一列 | 2 條紅 |
  | M4 懶載入改成首屏載入（boot 就抓 risk.json） | 1 條紅（第一版沒紅：檢查漏了大小寫，已改成不分大小寫） |
  | M5 既有燈號邏輯被改（門檻 25 改 30） | 1 條紅（釘住的 fixture） |
  | M6 距高點橫條刻度改錯（用 100% 當刻度） | 1 條紅 |
  | M7 「反向最強」門檻改成 0 | 1 條紅 |
  | M8 「最不相關」改成取最負的 | 1 條紅 |
  | M9 跳轉頁改成真的刪掉 | 1 條紅 |
  | M10 導覽列少了「分析」 | 1 條紅 |
  | M11 docs/ 塞一個超過上限的檔（ANALYSIS.md 尾端塞 130 KB） | 1 條紅 |
  | M12 大小上限的掃描器整個關掉 | 1 條紅 |

- 截圖（1200 與 390 寬）：改前／改後儀表板、分析分頁、矩陣在 390 寬的橫向捲動、展開的卡片三張（00646 有長歷史、黃金存摺只有價差、人民幣存摺「沒有分析項目」），見驗收報告。
- 對外請求 0；Python 與排程一字未改；隱私掃描與擋字串綠。

**怎麼退回**

```bash
git revert --no-edit 2520c18..stopA1-5          # 合併前：在分支上把 A1-5 的全部 commit 反轉
git revert -m 1 bc028ac && git push        # 合併後：退整個 A1-5（2026-09-26 合併）
```
退回後分析分頁與卡片的摺疊區消失、`analysis-debug.html` 變回原本的表格頁；資料檔不受影響。

---

## 2026-09-25 · 分析系列 A1-3 試算清單（標籤 `stopA1-3`；合併排在 A1-2 第二段驗收之後）

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/analyze.py` | 新增 `--adhoc 代號 --asset-class 類別 [--expense-ratio 值] --out 倉庫外目錄` 試算入口（`build_parser()`，參數名有測試釘住；`--slot` 在 adhoc 模式不用給）。三層結構性禁寫：`--out` 落在倉庫裡就在連網之前結束碼 2；adhoc 模式設 `WRITE_ROOTS`，`write_json`／`save_long`／`save_nav` 都先 `assert_write_allowed`，不在 out 底下就丟 `WriteRefused`；adhoc 的 `status.json` 也只寫到 out。算法沿用 risk.json 那一套：年化波動 1／5 年、最大回檔、目前距高點、對每個公開標的各算一次 3 年週報酬相關（列前三名；同代號在公開清單就略過不跟自己算）；上市未滿 3 年相關與 5 年波動寫資料不足；估值與折溢價一律資料不足；費用率標「使用者輸入」；固定稅務註記。便士：`meta.currency` 完全等於 `GBp` 才 ÷100 並記錄，`GBP` 不動。代號查無（Yahoo 404）明確失敗、不產檔。結果寫 `<out>/<代號>/<執行日>.json`（同一天重跑覆蓋）並更新 `<out>/index.json`（每個代號的最新檔與摘要數字） |
| `scripts/test_analyze.py` | 62 → 74 條：便士、英鎊與美元不動、`--out` 在倉庫裡連網前就擋、寫檔允許清單、查無不產檔、未滿 3 年、同日覆蓋、status 只到 out 且跑前跑後 `git status` 一樣、同代號略過、輸入檢查、介面釘住（含範本檔的四個參數、`persist-credentials: false`、`contents: write`、七類下拉）、正式模式仍要 `--slot`。fixture 代號一律 FAKE.* |
| `js/storage.js` | `listDir(path)`：contents API 對目錄回陣列，只留 name／type／path／sha；不存在回空陣列；非私人倉庫模式回 `entries: null` |
| `js/analysis-debug.js`、`analysis-debug.html` | 「試算」區：按一下先讀 `adhoc/index.json`（1 個請求）列表，點「看詳細」才讀那一檔；沒有 index 用 `listDir` 列目錄當備援（每個代號取檔名最新一筆，最多 20 個代號）；狀態：未設定／還沒有任何試算／有 index／備援清單／詳細／讀不到；固定稅務註記。代號只出現在瀏覽器裡 |
| `scripts/test_analysis_debug.html`、`scripts/test_analysis_debug_js.py` | 頁面檢查 13 → 16 項：有 index 的清單、無 index 的備援清單與空狀態、詳細頁（便士說明、使用者輸入標籤、相關前三名、資料不足原因、稅務註記） |
| `scripts/test_analysis_guards.py`、`scripts/sensitive_terms_hmac.json` | 17 → 20 條：公開 `.github/workflows/*.yml` 不得出現 `--adhoc`；fixture 代號要 FAKE 開頭；離線跑一次 adhoc、產出的檔也掃。具名字串掃描提速：HMAC 清單每筆多存 `cjk` 旗標（只是「有沒有中文字」），含中文的字串只對蓋到中文字的片段算；掃過的內容以雜湊記在鹽旁邊的 `scan-cache.json`（倉庫外），改過的檔才重掃——兩分多鐘變十秒 |
| `scripts/probe_analysis_sources.py`、`scripts/test_probe_analysis.py` | 探測加 `adhoc` 組：Y-16 用 ISF.L（iShares Core FTSE 100 UCITS ETF，倫敦掛牌；**格式探測用、非追蹤標的**）看 `meta.currency`；106 → 108 條 |
| `docs/adhoc-workflow.example.yml`（新） | 私人倉庫 invest-data 用的 workflow 範本：只有 `permissions: contents: write`；checkout 公開倉庫 main 唯讀、不另外給 token、`persist-credentials: false`；代號經環境變數傳、不拼進指令；結果 commit 訊息不含代號；查無代號時 workflow 紅、不產空結果 |
| `docs/ANALYSIS.md`、`README.md` | 5.8 試算清單、5.7 資料條款補一條、第 6 節改 A1-4（淨值回補）起；README 檔案結構、進度、「試算清單怎麼用」三步驟 |
| `index.html`、`history.html`、`records.html`、`settings.html`、`analysis-debug.html` | 只有 `bump_assets.py` 的版本號 |

**怎麼驗的**

- 單元測試 **384 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`；頁面測試用 Chrome）。
- 探測 adhoc 組在分支上真的跑了一次（`gh workflow run probe-analysis.yml --ref feat/stopA1-3 -f only=adhoc`，run 36118690062，2026-09-25 17:29 台北，1 個請求）：Y-16 可用，`meta.currency` 是 **`GBp`**、exchange LSE、最新值 1044.8 便士＝10.448 英鎊、926 根（2009-01-01 起）。台銀 0 次。
- 筆電真跑一次試算（在 worktree 裡、`--out` 指到倉庫外的暫存目錄）：`--adhoc VT --asset-class index_etf --expense-ratio 0.06`——1 個請求、結束碼 0；952 根（2008-06-23 起）、USD、1 年波動 12.89%、5 年 15.56%、最大回檔 −49.64%（2008-06-23 → 2009-03-02）、距高點 −2.28%、相關前三名 gspc 0.961／sp500tr 0.961／nvda 0.63；out 裡只有 `VT/2026-09-25.json`、`index.json`、`status.json`；**跑前跑後 `git status --porcelain` 完全一樣**。
- 突變對照組（改壞 → 只跑指定測試檔 → 必須紅 → 還原）**19 組全部符合預期**（4 組基準綠、15 組紅）：

  | 改壞的方式 | 結果 |
  |---|---|
  | M1 禁寫第一層：拿掉「--out 不可以在倉庫裡」的檢查 | 1 條紅 |
  | M2 禁寫第二層：寫檔函式略過允許清單 | 1 條紅 |
  | M3 禁寫第三層：公開 workflow 出現 --adhoc | 1 條紅 |
  | M4 便士 ÷100 拿掉 | 1 條紅 |
  | M5 GBP（英鎊）誤當便士 | 1 條紅 |
  | M6 代號查無仍產檔 | 1 條紅 |
  | M7 上市未滿 3 年也硬算相關 | 1 條紅 |
  | M8 adhoc 的 status.json 寫回 data/analysis | 7 條紅（第二層保險擋下，連結果檔都不產） |
  | M9 同一天重跑不覆蓋、多產一份 | 2 條紅 |
  | M10 index.json 不更新 | 2 條紅 |
  | M11 介面改名（--asset-class 改成 --class）、範本沒跟著改 | 1 條紅 |
  | M12 檢視頁：備援清單少列代號 | 1 條紅 |
  | M13 檢視頁：詳細頁少了「使用者輸入」標籤 | 1 條紅 |
  | M14 探測：Y-16 的 note 不帶 currency | 2 條紅 |
  | M15 fixture 代號不是 FAKE 開頭 | 1 條紅 |

- 守門：公開 workflow 一字未改（`update-data.yml`、`probe-analysis.yml`、`probe-bot.yml`）；隱私掃描綠；fixture 代號一律 FAKE.*。
- 截圖（假 fixture）：有 index 的清單、無 index 的備援清單、詳細頁，見驗收報告。

**怎麼退回**

```bash
git revert --no-edit 936b28c..stopA1-3          # 合併前：在分支上把 A1-3 的全部 commit 反轉
git revert -m 1 6292af2 && git push        # 合併後：退整個 A1-3（2026-09-25 晚上合併，A1-2 第二段通過之後）
```
退回後私人倉庫的 workflow 會因為公開倉庫沒有 `--adhoc` 而失敗（結束碼 2），私人倉庫已有的 adhoc/ 結果不受影響。

---

## 2026-09-25 · 分析系列 A1-2 成本＋集中度（標籤 `stopA1-2`）

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `scripts/probe_analysis_sources.py`、`scripts/test_probe_analysis.py` | 探測加 `retest` 組（分支上跑、workflow 一字未改）：N-04 證交所 all_etf.txt 改正式標頭＋Referer、N-05a 先拿 e添富頁面 cookie → N-05b 才 POST（頁面被擋就不 POST）、Y-14 `^SP500TR` 週線（period1=345600 星期一）、Y-15a/b 黃金現貨兩個代號（第一個 404 才發第二個）；「安全性考量」擋頁不管 HTTP 200／307／502 一律判失敗。離線測試 100 → 106 條 |
| `scripts/analyze.py` | 新增成本 `data/analysis/cost.json`：00646 追蹤差（主基準 `^SP500TR` 總報酬、對照 `^GSPC`；累計差＝00646 台幣報酬 −[(1＋基準)(1＋匯率)−1]，年化幾何；1 年／3 年視窗、ISO 週對齊、起點缺了往前找最多 3 週、輸出實際起訖日期）；折溢價每個交易日累積一列到 `data/analysis/nav/<id>.json`（all_etf.txt 正式抓法；「預估」＝當筆成交價對投信盤中預估淨值、「確定」＝前一營業日收盤對官方淨值、隔天回填；分母都是淨值；滿 20 個交易日才給中位數；被擋寫「未接」，00679B 退路櫃買 30 日標「僅 30 日」）；黃金存摺價差＝(本行賣出−本行買入)÷中價；條塊溢價含金鑽 1 台兩＝37.5 公克；靜態費用表只引用不產生。`^SP500TR` 進 `EXTRA_LONG_SERIES`（`data/history-long/sp500tr.json`，不在 assets.json）。`run(paths=…)`／`default_paths()`：所有讀寫可指到倉庫外（A1-3 預留）。risk.json 的 `asOf` 改名 `dataThrough`（撞到設定檔鍵名）。拆解的 notes 加「殘差為什麼常是負的」（期貨基差） |
| `scripts/test_analyze.py` | 48 → 61 條：追蹤差（含匯率換算、中價、往前找起點、資料不足、缺基準）、all_etf 解析與「確定」回填到前一交易日、分母是淨值、滿 20 天才有中位數、黃金價差、條塊台兩、櫃買日期補年份、正式標頭、被擋不假裝接了、`paths` 字典只寫到指定位置、離線跑會產 cost.json |
| `scripts/publish.py`、`scripts/test_publish.py` | 雲端擁有清單加 cost.json、static-costs.json、nav/ 兩檔、history-long/sp500tr.json |
| `js/concentration.js`（新） | 集中度純函式：`validate`（負數／非數字擋下、總和不在 95～105 只警告）、`facts`（最大單一類別、前二合計、與收入代理同向的合計；門檻 0.5、顯示相關係數本身；`other` 不入同向）、`fromForm`／`toFormValues`／`emptyTemplate`。設定檔的三個鍵名只准出現在這個檔 |
| `js/storage.js` | 加 `loadFile(path)`（回 `{data, sha, path}`，404 → `data:null`）與 `saveFile(path, data, message)`；只有私人倉庫模式可用；既有 `ghLoad`／`ghSave` 改成呼叫同一組函式 |
| `js/analysis-debug.js`、`analysis-debug.html` | 檢視頁加成本區（追蹤差兩個口徑、折溢價、價差、條塊、靜態費用表）與集中度區（未設定／有設定／表單；表單欄位 id 用 `w_<類別>`；先測連線再存；commit 訊息「analysis-profile 更新（時間）」不含數字；不 console.log 任何值）；預留 `<section id="adhoc">`；風險表改讀 `dataThrough` |
| `scripts/test_analysis_debug.html`、`scripts/test_analysis_debug_js.py` | 13 項頁面檢查（Chrome 無頭）：成本區有畫、三個事實 30／50／30、`other` 不在同向列表、未設定狀態、表單驗證、設定檔鍵名不進 DOM、storage 有 loadFile／saveFile、腳本順序與 adhoc 區 |
| `scripts/test_analysis_guards.py` | 14 → 17 條：具名字串（HMAC）掃描對 data/ 的 JSON 只掃鍵名與字串值、去重（長歷史逐字算 HMAC 要一分多鐘）；`js/concentration.js` 進掃描清單；設定檔鍵名（分開寫的字串）只准在 `js/concentration.js`；**離線跑一次 analyze、產出的每個 JSON 也掃**（禁用鍵名、判斷用語、設定檔鍵名）；掃描器自己的對照組 |
| `data/analysis/static-costs.json`（新，手抄） | 11 筆：00646／00679B 的經理費與保管費分級費率（元大投信官網）、最近一年總費用率（投信投顧公會各項費用比率頁，2025 全年：00646 0.36％、00679B 0.14％；查詢條件寫在出處欄）、證券交易稅（股票千分之三、ETF 千分之一、債券 ETF 停徵至 2026-12-31；全國法規資料庫）；「目前適用級距」留 null 並寫原因；每筆帶網址、查核日期、標籤「官方公告」 |
| `data/analysis/cost.json`、`data/analysis/nav/tw00646.json`、`data/analysis/nav/tw00679b.json`、`data/history-long/sp500tr.json`、`data/analysis/status.json`／`risk.json`／`decompose.json` | 種子資料：筆電沙盒 2026-09-25 11:40 的一次真實跑（11 次請求）；合併後由雲端 15:30 那一輪接手維護。status／risk／decompose 也一起換成這一次的（risk 的欄位已是 `dataThrough`） |
| `docs/ANALYSIS.md`、`README.md` | 5.5 成本、5.6 集中度、5.7 資料條款（證交所／櫃買／靜態表）、第 6 節改 A1-3 起；三個錯字（籌碼、矩陣、口徑）；README 檔案結構、進度、回滾表 |
| `index.html`、`history.html`、`records.html`、`settings.html`、`analysis-debug.html` | 只有 `bump_assets.py` 的版本號（20260925-1） |

**怎麼驗的**

- 單元測試 **364 條全綠**（`python -m unittest discover -s scripts -p "test_*.py"`；頁面測試用 Chrome：`IW_BROWSER="C:\Program Files\Google\Chrome\Application\chrome.exe"`）。
- 探測 retest 組真的跑了一次（分支上 `gh workflow run probe-analysis.yml --ref feat/stopA1-2 -f only=retest`，run 36084006372，2026-09-25 09:54 台北，6 個請求）：N-04／N-05a／N-05b／Y-14 可用，Y-15a／Y-15b 404（黃金現貨拿不到）。台銀 0 次。
- 筆電沙盒真實跑兩次（worktree 副本，真倉庫不碰）：11:24（11 次請求、結束碼 0、cost.json 產出）與 11:40（換上最終版程式與費用表，再 11 次）。追蹤差 1 年：00646 22.27% vs `^SP500TR` 台幣 22.28%（差 −0.01%）、3 年年化 −1.02%；折溢價 00646 預估 +0.23%（9/24）、確定 +0.96%（9/23）；存摺價差 1.19%；條塊溢價 1.35%（1 公斤）～2.13%（1 台兩）。
- 突變對照組（改壞 → 只跑指定測試檔 → 必須紅 → 還原）**30 組全部符合預期**（4 組基準綠、26 組紅）：

  | 改壞的方式 | 結果 |
  |---|---|
  | M1 追蹤差的幣別換算改壞（基準只用美元報酬） | 1 條紅 |
  | M2 追蹤差用即期賣出而不是中價 | 1 條紅 |
  | M3 追蹤差不給視窗起訖日期 | 2 條紅 |
  | M4 折溢價分母改錯（用價格） | 2 條紅 |
  | M5 預估與確定的標籤對調 | 1 條紅 |
  | M6 官方淨值填到今天而不是前一交易日 | 1 條紅 |
  | M7 不到 20 個交易日也顯示中位數 | 1 條紅 |
  | M8 被擋時把舊資料當成接了 | 1 條紅 |
  | M9 all_etf 不帶正式標頭（拿掉 Referer） | 1 條紅 |
  | M10 台兩的公克數改壞 | 1 條紅 |
  | M11 黃金價差分母改成本行賣出價 | 1 條紅 |
  | M12 拆解 notes 少了期貨基差的解釋 | 1 條紅 |
  | M13 paths 字典沒被尊重（status.json 寫回模組常數的位置） | 1 條紅 |
  | M14 基準序列 ^SP500TR 從清單拿掉 | 2 條紅 |
  | M15 other 混進同向計算 | 1 條紅（第一版沒紅：other 沒有代理標的、混進去結果也一樣，已補「other 不得出現在同向列表」的檢查） |
  | M16 同向門檻比較改壞（> 0 就算同向） | 1 條紅 |
  | M17 表單欄位改用設定檔的鍵名當 id | 1 條紅 |
  | M18 驗證：負數也放行 | 1 條紅 |
  | M19 驗證：總和不在 95～105 變成錯誤而不是警告 | 1 條紅 |
  | M20 設定檔鍵名出現在公開輸出（cost.json 多印一個鍵） | 1 條紅（第一版沒紅：守門只掃倉庫裡現成的檔；已補「離線產出的檔也掃」） |
  | M21 守門：設定檔鍵名的檢查整個關掉 | 1 條紅（第一版沒紅：掃描器沒有對照組；已補） |
  | M22 探測：N-05b 不等 N-05a 就 POST | 1 條紅 |
  | M23 探測：Y-15b 不管 Y-15a 結果都發 | 2 條紅（第一版只被別條抓到；已把兩個方向都釘進同一條） |
  | M24 publish 擁有清單少了 nav 檔 | 1 條紅 |
  | M25 補抓判斷改壞成每天抓（tag 之後的修正） | 1 條紅 |
  | M26 「最近一個完成週」少減 7 天（tag 之後的修正） | 2 條紅 |

- 守門：`data/analysis/*.json`、`data/history-long/*`、頁面靜態內容、畫出來的 DOM 都掃過設定檔鍵名——0 次；分析系列檔案沒有判斷用語（第一版全套跑出 `analyze.py` 一個裸的「賣出」，已改成牌價欄位名「本行賣出」）。
- 截圖（假設定檔 10／20／30…）：成本區、未設定、有設定＋表單，見驗收報告。
- **補抓規則修正在 tag 之後的 commit 2a53ff8，同一個合併**：A1-1 的「最後一根超過 7 天才補抓」實際上從星期二到下週一每天都會補抓（最後一根永遠是上週一、年齡 8～13 天），一天 9 次 Yahoo 請求。改成確定性判斷：最近一個已完成週的週一＝今天所在 ISO 週的週一減 7 天，存檔最後一根早於它才補抓——每週只在週一那次 review 抓一次，週一沒跑到週二自動補。測試：連續 14 天每天跑一次 review 恰好抓 2 次；週一休市的週二週棒、匯率的週五點都算「已經有」。突變 M25（判斷改成每天抓）、M26（少減 7 天）各 1～2 條紅。ANALYSIS.md 5.1 那句改成真的。

**怎麼退回**

```bash
git revert -m 1 d398775 && git push        # 退整個 A1-2（合併後；含 tag 之後的補抓修正）
git revert --no-edit 9ea8f65..stopA1-2           # 或在分支上逐個 commit 反轉（探測 + 主體）
```
退回後雲端下一輪不再產生 cost.json 與 nav/，已產生的檔案可另外刪；risk.json 的欄位會變回 `asOf`（檢視頁舊版讀得懂）。

---

## 2026-09-24 · A1-1 資料層＋風險＋拆解（assetClass、國際金價、週線長歷史、risk／decompose、檢視頁、守門）

**為什麼要做**：分析系列 A1～A4 的每一個面向都要 10 年等級的長歷史，這個專案原本只存 400 點日線；
風險面向（波動、回檔、相關）全部是事實、不含預測，先做它最不會出錯；黃金與 00646 的拆解則是把
「台幣價格裡有多少是匯率」講清楚，是之後估值與成本面向的基礎。這一段照使用者的裁決拆成 A1-1／A1-2 兩段，
A1-2（成本、集中度）另外一段。

**改了什麼**

| 檔案 | 內容 |
|---|---|
| `data/assets.json` | 14 項全部加 `assetClass`（gold_tw／index／stock／index_etf／bond_etf／commodity／crypto／fx，跟 `type` 並存）；新增雲端資產 **gold_intl**（國際金價，COMEX 近月期貨 GC=F，type yahoo、owner cloud、group 貴金屬、`cadence: full`＝只在 09:30／11:30／13:35／15:30 更新）；`types` 說明補 assetClass 與 cadence 的新規則 |
| `data/schedule.json` | `fieldNotes.cadence` 補：目前 cadence=full 的四項與 gold_intl 的節奏（過期判定只看 full.at 那幾個點） |
| `scripts/fetch_data.py` | 新增 `skip_for_cadence()`：`--light` 遇到 `cadence=full` 且上一次成功 → 沿用上次結果；上一次失敗或沒抓過 → 照抓。主迴圈在呼叫 handler 之前統一判斷，實體條塊與 FinMind 匯率原本在 handler 裡的那兩條留著當第二道保險 |
| `scripts/net_policy.py` | 新增。主機白名單（台銀永遠拒絕、帶帳密的網址不算數）從探測腳本抽出來，探測與分析共用；多一個 `response_hook` 讓轉址的每一跳都被檢查 |
| `scripts/probe_analysis_sources.py` | 改成 `import net_policy`，行為不變（100 條測試照跑） |
| `scripts/analyze.py` | 新增。只在 review 那一輪跑：(1) 維護 `data/history-long/<id>.json`——Yahoo 週線（`period1=345600`＝1970-01-05 星期一、`period2=<現在>`、`interval=1wk`；檢查宣告粒度是 1wk **且** 時間戳中位間距 6～8 天；起始＋7 天 > 現在的進行中週不收；最後一根完成週棒超過 7 天才補抓）＋ USD/TWD 週線（FinMind，2006 起，每 ISO 週取最後一個營業日，抓取當週不收，標原始出處）；(2) `risk.json`：年化波動 52／260 週（缺 >10% 資料不足）、最大回檔（整段、含高低點與回到高點的日期）、目前距高點、3 年相關矩陣（ISO 週對齊、成對可用、重疊 <100 週資料不足、對角線≠1 會喊）、00679B 的 10 年視窗寫「資料不足，2027-01 起才滿」；(3) `decompose.json`：台銀金價 ＝ GC=F × USD/TWD 中價 ÷ 31.1035 ＋ 殘差（同一天與前一日兩個口徑、每日序列、60 日摘要、latest.json 的即時一列）、00646 月報酬 ＝ (1＋S&P)(1＋匯率) − 1 ＋ 殘差（還沒走完的當月不算）；(4) `status.json`：上次執行、ok、errors、每個長歷史做了什麼、請求數，錯誤訊息洗掉本機路徑。結束碼 0／2／1；資料標籤照裁決用文字（估算／單一來源／有對照／多來源一致），★ 只出現在方法文件 |
| `scripts/publish.py` | `owned_paths()`：雲端多擁有 `data/history-long/<雲端標的>.json` 與 `ANALYSIS_PATHS`（status／risk／decompose），競態重試時才不會被丟掉 |
| `.github/workflows/update-data.yml` | 報告之後多一步「分析（只在 review 那一輪）」：`if: mode == full && slot == review`、`continue-on-error: true`；結束碼 2 印 `::warning::`、其他非 0 印 `::error::` 但**下面照樣存檔**；執行摘要多印分析狀態一行。觸發方式與時刻都沒改 |
| `js/app.js` | 「台灣白天海外市場尚未收盤」那句提示改看 `type === 'yahoo'`（不看 group，國際金價在貴金屬組） |
| `index.html`／各 HTML | 頁尾加「分析檢視」連結（A1-5 起改為分析分頁）；`bump_assets.py` 版本 20260924-1 |
| `analysis-debug.html`、`js/analysis-debug.js` | 新增。檢視頁（A1-5 起併入分析分頁）：最上面先顯示分析上次執行時間與最新錯誤，再用純表格列 risk／decompose，每一格帶資料日期與資料標籤；讀不到檔要紅字說讀不到 |
| `docs/ANALYSIS.md` | 新增。公開版方法文件：兩套等級（方法 ★／資料文字標籤）的定義並列、誠實規則、五個面向的方法清單、assetClass 對應與自動套用、A1-1 已實作的定義、資料條款、A1-2 起的預告。只涵蓋公開標的，沒有任何個人資料 |
| `scripts/test_analyze.py`（48）、`scripts/test_cadence.py`（10）、`scripts/test_analysis_guards.py`（12）、`scripts/test_analysis_debug.html`＋`scripts/test_analysis_debug_js.py`（11）、`scripts/test_publish.py`（+1）、`scripts/test_schedule_util.py`（cadence 清單加 gold_intl）、`scripts/test_probe_analysis.py`（第二道保險的測試改成改 `net_policy` 的清單） | 離線測試全部不連網 |
| `scripts/sensitive_terms_hmac.json` | 新增。隱私掃描的具名字串清單——只存長度與**加鹽 HMAC-SHA256**，鹽放在倉庫外（預設 `../iw-private/scan-salt.txt`，環境變數 `IW_SCAN_SALT_FILE` 可指定）；沒有鹽（GitHub runner）就跳過這一段，通用樣式與禁止鍵名永遠掃 |
| `README.md` | 標的 13→14（國際金價一列）、檔案結構、回滾表、進度 |
| 本機 `PLAN.md`（不進倉庫） | 7.6 補週線抓法與 period1 的坑、7.11 補匯率長歷史、7.12 註記 A1-1 已接的來源；改前備份 `backup/PLAN.md.before-A1-1` |

**怎麼驗證的**

- **順序規則**：先寫離線測試、綠了才動真來源。這一段對真來源的請求：沙盒實跑兩次（各 1 個 Yahoo 日線＋9 個 Yahoo 週線＋1 個 FinMind）＋ 1 次對齊檢查（2 個 Yahoo）＝ **24 個請求，台銀 0**；全部在 worktree 的沙盒副本裡跑，真倉庫沒有寫入任何資料檔。
- 全套測試 **333 條全綠**（既有 251＋新 82）。瀏覽器測試改用 Chrome（`IW_BROWSER`）：這台的 Edge 這幾天無頭模式對任何頁面都回空輸出（版本目錄有兩個、像是更新待重啟），Chrome 正常。
- **突變對照 32 個**（工作區複製到暫存目錄→改壞一處→只跑對應的測試檔→必須紅）：3 個基準綠、**29 種改壞全部紅**——

  | 改壞的方式 | 結果 |
  |---|---|
  | 波動率視窗改壞：不夠 90% 也硬算 | 1 條紅 |
  | 波動率年化用 √252 不是 √52 | 1 條紅 |
  | 相關矩陣把自相關≠1 放過 | 1 條紅 |
  | 相關矩陣重疊不足也照算 | 1 條紅 |
  | 拆解公式的盎司→公克改成常衡盎司 28.35 | 3 條紅 |
  | Yahoo 週線不看宣告的粒度／不看實際間距／進行中的當週也收 | 1／1／2 條紅 |
  | 補抓規則改壞（每天重抓整段） | 2 條紅 |
  | 匯率週線不是取每週最後一個營業日 | 1 條紅 |
  | 白名單拿掉台銀那一條（分析測試）／（A0 探測測試） | 1／2 條紅（第一輪這一項**沒有紅**：白名單抽到 net_policy 後，A0 那條測試改的是探測腳本 import 進來的名字，碰不到規則本體——補了直接改 `net_policy.ALLOWED_HOSTS` 的測試、也把 A0 那條改成同樣寫法，第二輪才紅） |
  | PolicedFetcher 不查主機 | 1 條紅 |
  | 算不出來時 status.json 不寫錯誤／錯誤訊息不洗路徑 | 1／1 條紅 |
  | 10 年視窗用 520 週算（00679B 會被說成 2026-12 就夠） | 1 條紅 |
  | 隱私掃描：持倉數字樣式拿掉／禁止鍵名清空／HMAC 永遠不中 | 1／1／1 條紅 |
  | 擋字串：判斷用語清單清空／在 analyze.py 塞一個判斷用語／在 ANALYSIS.md 塞一個假持倉數字／私人目錄從 .gitignore 拿掉 | 1／1／1／1 條紅 |
  | 通用 cadence 判斷拿掉／不看上一次成功與否 | 2／2 條紅 |
  | publish 擁有清單少了分析檔 | 1 條紅 |
  | 檢視頁：日期欄不標日期／塞一個判斷用語／沒有 status 時不喊 | 1／1／1 條紅 |

- **沙盒實跑**（最終版程式，2026-09-24 00:47）：先抓 gold_intl 日線（252 點）→ merge → analyze：10 個長歷史全部整段回補（^GSPC 2,959 週自 1970-01-05、^TWII 1,505、2330 1,394、00646 563、00679B 506、NVDA 1,444、BTC 627、CL=F 1,361、GC=F 1,360、USD/TWD 1,070 週自 2006-01-06），每檔都略過進行中的 2 根（當週＋Yahoo 附帶的即時點），合計 **836 KB**（最大 ^GSPC 189 KB；覆述時估 650 KB，實際多三成——每點多了 dateSource 欄與較長的日期）；status ok、0 錯誤、對外 10 個請求。risk：對角線全部 1；00679B 的 10 年視窗寫「資料不足，2027-01 起才滿 10 年」。decompose：黃金殘差近 60 日中位數 −0.3%～−0.4%（台銀價格略低於公式值，殘差含期貨基差與時間差）、即時一列殘差 +0.17%；00646 129 個月、近 12 個月殘差累計 +1.35%、月殘差標準差 0.91%。守門掃描對這些真實輸出也綠。
- **第一次實跑抓到、第二次才修好的**：(1) Yahoo 把「一週」的起點對齊 `period1` 那一天的星期幾——`period1=0` 是 1970-01-01 星期四，歷史夠長的 ^GSPC 就變成週四起的週（2,959 根全是星期四），其他標的因為歷史起點較晚仍是星期一；用 1 個對照請求證實 `period1=345600`（星期一）後全部對齊，程式改成一律用星期一（A0 學到的「period1=0」在這裡要修正）。(2) 匯率週線收進了還沒走完的當週（9/23）；(3) 00646 月拆解收進了還沒走完的 9 月（讓月殘差標準差從 0.9% 被抬到 2.3%）。三項都補了測試與對照組。
- 隔天 15:30 review 實跑後的檔案內容摘要與 gold_intl 卡片：見驗收回報（合併之後才會有）。

**怎麼退回**

- `git revert -m 1 <這次的合併 commit> && git push`（它是 7459b34 之後的第一個合併）。退回後雲端下一輪就不再跑分析、不再產生 `data/history-long/` 與 `data/analysis/`；已產生的檔案不會自己消失，要的話另外刪。gold_intl 的卡片與 `data/history/gold_intl.json` 也隨 assets.json 一起退回。
- 標籤 `stopA1-1` 打在分支上的最後一個 commit（`git log --oneline stopA1-1 -1` 可查）。
