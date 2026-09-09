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

**還沒做（要等你）**：7-H（cron-job.org 建好後觀察 2 小時）、7-I（隔天四份報告準時到）。

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

**還沒做（明天）**：7-H、7-I 用 `python scripts/verify_schedule.py --date 2026-09-10` 彙整。
