---
name: iw-autopilot
description: InvestWatch「自動駕駛」的流程。David 輸入「自動駕駛：<階段>」之後（hook 會提示）照這份流程自己跑完一個階段——偵察、覆述、審查、施工、測試、驗收、停在合併前寄信；他輸入「放行／修改／繼續 <階段>」之後也照這一份接著做。
---

# 自動駕駛

## 鐵則（先看這七條；壓縮對話之後也只會留下最前面這一段）

1. **合併進 main 之前一律停。** 只有 David 親手輸入「放行 <階段>」、而且 hook 明確告訴你「已經開了通行證」，才能合併。沒有那句 hook 的話＝沒有放行。
2. **每一次停下來，先寄信再停。** `python .claude/hooks/iw_notify.py send --stage <階段> --kind <stop|tier2|ready> --report <檔>`。寄不出去就照實寫在最後的回覆裡。
3. **被擋下＝停止條件，不是障礙。** 不換寫法繞過、不重試、不改保護檔、不找別的工具做同一件事。寫停止報告、寄信、停。
4. **不確定該不該自己決定的事，就不要自己決定**（見下面停止條件 2）。
5. 模型固定 `claude-fable-5-1`、思考強度 `xhigh`。不對時 hook 會擋；不要試著自己切換。
6. 只在這個階段的 worktree、`.autopilot/`、暫存資料夾寫東西；主目錄只能看與快轉。
7. 專案原本的鐵則全部照舊（隱私、誠實、不給行動指示、架構、偵察、流程）：見同資料夾的 `review-criteria.md` A 節。

## 九個停止條件（任一成立就：寫停止報告 → 寄信 → 停）

1. 準備合併到 main（正常的終點，`--kind ready`）。
2. 規格沒寫到，而且有兩種以上合理做法會影響 David 看到的結論（門檻、分類規則、口徑、狀態詞）。
3. 要動排程、workflow、`data/schedule.json`、`scripts/update_local.ps1`、資料來源、私人倉庫、PAT、隱私相關的東西，或自動駕駛自己的檔。動到「第二層」的檔（`.claude/autopilot/config.json` 的 tier2）＝改完、測完就寄 `--kind tier2`，等他輸入「繼續」才能推。
4. 要刪資料、改寫歷史點、動既有標籤。
5. 同一個測試或錯誤修兩次仍失敗。
6. 對外請求超出規格的估計，或來源被擋。
7. 審查代理兩輪後仍不批准。
8. 需要清單以外的工具或程式（被擋下、被拒絕批准都算）。
9. 超過時間上限（8 小時，從 David 每次輸入起算）、用量上限、模型或強度被換。

## 指令怎麼寫才不會被擋

- 用簡單的指令。任何邏輯都寫成 Python 腳本檔（放這個階段的 worktree 或暫存資料夾）再用 `python 腳本.py` 執行——**不用 `python -c`、不用 heredoc 餵程式碼**（腳本要留得下來，審查代理與 David 才看得到）；`python -m` 只能跑 `unittest`、`http.server`、`json.tool`、`py_compile`。不要用 `case`、函式定義、`eval`、`source`、別名、`xargs`、`find -exec`、大括號展開（`{a,b}`）、`IFS`、把指令用管線餵給 `bash`。
- 變數先在同一段指令裡用字面值指定（`PY="…/python.exe"` 之後才 `"$PY" …`），一行一個、不要放在 `&&` 後面或 `if`／`for` 裡面；用的時候加引號。
- 會改檔的指令（`rm`、`mv`、`cp`、轉向 `>`）：路徑寫字面值或上面那種變數，不要用迴圈變數、指令的輸出、萬用字元指到主目錄。
- 不用 PowerShell 工具、不用「問問題」的工具、不排程、不開審查代理以外的子代理、不用瀏覽器擴充。
- 啟動時 hook 若說「分支在啟動之前就已經改了動不得的檔」：那些是 David 在場時改的，你不能再動；停止報告「你要決定的事」第一件寫明這次改了保護或排程相關的檔。
- git 都在 worktree 裡下；主目錄只下唯讀指令、`git fetch`、`git merge --ff-only origin/main`。
- `gh` 只能看執行紀錄；寄信一律用 `iw_notify.py`。對外抓資料只走專案自己的抓取程式。

## 流程

**0. 啟動後**　`python .claude/hooks/iw_notify.py status` 看狀態；讀規格（hook 提示裡的路徑；審查代理看的也是那一份）；`.autopilot/runs/<階段>/` 是這個階段的資料夾。之前做到一半的話，從那裡的檔與狀態接著做。

**1. 偵察**　只讀本機檔與官方說明文件頁；不打任何回資料的端點。

**2. 覆述**　寫 `.autopilot/runs/<階段>/01_覆述.md`：我理解的需求、偵察到的事實、做法、規格沒寫到而我打算怎麼做（每一條都要標出來）、檔案清單與行數估計、對外請求估計、測試與突變計畫、不做的事。

**3. 審覆述**　呼叫審查代理（`subagent_type: iw-reviewer`，不帶 `model`）。提示的開頭三行固定：

    REVIEW-KIND: restatement
    STAGE: <階段>
    COMMIT: none

批准 → 施工。要改 → 改完再審，最多兩輪。升級、或兩輪不過 → 停（條件 2 或 7）。結論是 hook 記的，不要自己轉述成別的。

**4. 施工**　worktree：`git worktree add --no-track -b feat/stop<階段> .claude/worktrees/stop<階段> origin/main`。離線測試先綠 → 突變對照（每條規則都要有「改壞→紅」）→ 截圖 → 文件與 CHANGELOG 三段式 → 逐檔 `git add` → commit → 標籤 `stop<階段>` → 推分支與標籤。

**5. 驗收材料**　都放 `.autopilot/runs/<階段>/`：`02_驗收報告.md`、`diffstat.txt`（`git diff --stat origin/main...HEAD`）、`tests.txt`（全套測試的輸出）、`mutation.txt`、`03_停止報告.md`（照 `report-template.md`）。

**6. 審驗收**　同第 3 步，開頭改成 `REVIEW-KIND: acceptance`、`COMMIT: <worktree 的 HEAD，完整 40 碼>`。審完之後如果又改了任何東西，要重審。

**7. 停在合併前**　`python .claude/hooks/iw_notify.py send --stage <階段> --kind ready --report .autopilot/runs/<階段>/03_停止報告.md`。程式會自己檢查標籤、分支有沒有推、有沒有動到不能動的檔、審查紀錄；任何一項不過就不寄，照它說的處理。寄出後結束這一輪。

**8. 放行之後**（hook 會說通行證開了、只准合併哪個 commit）
1. `git fetch origin`；`git worktree add --detach .claude/worktrees/stop<階段>-merge origin/main`
2. 在那裡 `git merge --no-ff --no-commit feat/stop<階段>`（或通行證指定的那個 commit）→ 跑全套測試 → `git commit`
3. 推之前再 `git fetch origin`：`origin/main` 還是合併的第一個 parent 才推；不是就以新的為底重做第 2 步（不強推）
4. `git push origin HEAD:main` → 主目錄 `git merge --ff-only origin/main`
5. 文件 commit（只改 `README.md` 的回滾表與 `docs/CHANGELOG.md` 的合併紀錄，一筆，合併後 60 分鐘內）→ `git push origin HEAD:main` → 主目錄再快轉
6. 收 worktree 與分支（本機＋遠端）、停預覽伺服器、確認線上
7. `python .claude/hooks/iw_notify.py close --stage <階段>`；最後回報合併 commit 與退回指令

**9. 修改之後**（「修改 <階段>：＿＿」）　修正做在標籤之後的 commit、標籤不動 → 回到第 5 步。

## 回覆 David 的寫法

每一輪最後的回覆：一句白話說做到哪裡、為什麼停、他要輸入什麼；信寄出了沒有（沒寄出去要說）。細節放 `.autopilot/runs/<階段>/`。
