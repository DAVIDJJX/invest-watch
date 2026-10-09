# 第三方：驗收機與 Codex（SKILL.md 第 4b 步的細節；P2）

兩個施工方改不了的第三方：**驗收機**（GitHub 的電腦從實際的 commit 重跑全套測試、三種掃描、突變；`.github/workflows/verify.yml`＋`scripts/verify_ci.py`）與**外部審查員 Codex**（OpenAI 的程式碼審查，讀 PR 的實際 diff，只標 P0／P1）。兩者的結果由 hook 讀 GitHub，不由你轉述。

## 順序（本機全套綠、突變紅、commit、標籤、推分支與標籤之後）

1. **等驗收機**：`py -3.12 .claude/hooks/iw_notify.py verify --stage <階段> --wait`（每 60 秒查一次，上限 40 分鐘）。紅就先修（修完是新 commit，重推、重等）；結果檔會抓到 `.autopilot/runs/<階段>/verify/<commit>/verify-result.json`。紅的原因是「封鎖沒設成」（有一段沒有 3／3）或執行機排不到，不是測試紅：`gh run rerun <執行編號> --failed`——只准這個 commit 的驗收、最多 2 次、每次寫進報告；2 次還不過就寄 `--kind stop`。
2. **開 PR**（只准這一支開）：先寫 `.autopilot/runs/<階段>/pr-body.md`——只寫改了什麼，**不放規格原文**、不放本機路徑、不放任何個人資料——再 `py -3.12 .claude/hooks/iw_notify.py pr open --stage <階段> --title "停點 <階段>：一句話" --body-file .autopilot/runs/<階段>/pr-body.md`。程式先過隱私掃描才送。改標題內文用 `pr edit`——開 PR 之後又動到新的保護檔時一定要再 `pr edit` 一次（最後那一段清單由程式重列），不然寄 ready 時程式會說清單對不上。那一段要留在內文的最後、一字不改：後面不要再加字，也不要在 GitHub 網頁上手動改它（寄 ready 前程式比整段）。commit 訊息與標籤訊息也一樣不放個人資料、權杖、信箱、本機路徑：推送前的檢查會逐筆掃，命中就擋。這些掃描包含具名字串，要倉庫外的鹽；讀不到鹽就送不出、推不了——寄 `--kind stop`，信裡寫明鹽不在。
3. **等 Codex**：`py -3.12 .claude/hooks/iw_notify.py review-status --stage <階段> --wait`（上限 60 分鐘）。Codex 設了 Automatic review 會自己審；10 分鐘沒動靜就 `py -3.12 .claude/hooks/iw_notify.py pr request-review --stage <階段>`（留言內容寫死是 `@codex review`，之後有新 commit 要重審也只用這一句；自動審的算第 1 輪，輪數上限寫在設定，到了上限程式不再留言——寄 `--kind stop`，把剩下的意見列成表；要不要加開由 Cowork 決定，加開的那幾輪由 David 自己在 PR 上留言，怎麼收見 `docs/AUTOPILOT.md`「到了輪數上限怎麼收」）。Codex 有意見才發 review（訊號 A）；沒有意見時它不發 review，只把它自己那則「Codex Review Summary」留言更新成 Completed——程式認那張表（訊號 B：Commit 欄對得上最新的 commit、完成時間晚於推送與最後一則 `@codex review`）。表情不算。那張表的格式跟預期的不一樣時，程式判定未完成，照下面那一節處理，不要自己當成通過。
4. **回覆意見**：讀 `review-status` 印出來的 findings，寫 `.autopilot/runs/<階段>/03_第三方審查.md`，固定一張表、一行一條：

       | 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |
       |---|---|---|---|---|
       | 1234567 | P1 | js/app.js:42 | 採納並修 | commit abc1234 |
       | 1234568 | P1 | scripts/x.py:9 | 不採納 | 理由… |

   回覆那一欄只能是「採納並修」或「不採納」，一字不差（「尚未採納」「部分採納」都不算）；最後一欄不能空；同一條不能有兩種回覆——不符合的程式當成還沒回覆。
   Codex 的意見有兩種：行內留言（編號是一串數字），以及寫在 review 本文裡的（釘不到改動行上的意見；編號是「review 編號-第幾條」，例如 `5409995249-1`）。`review-status` 兩種都會列，兩種都要回覆。
   這個 PR 上 Codex 提過的每一條都要有，不分是哪個 commit 的；「採納並修」就修在標籤之後的 commit、重推、回到第 1 步（驗收機與 Codex 都要再過一次）。最新的 commit 被提了 P0＝還沒完，修了再審。別人的留言忽略（信裡會列出來）。沒標等級的意見當 P1。
   **不在 PR 上回覆、不爭論、不 resolve、不刪、不隱藏、不駁回**——守門會擋，那也不是我們的做法。`gh pr`／`gh issue` 只有確定只讀的子指令（view、list、diff、checks、status）放行，其他一律擋（含 `gh pr revert`）。
5. **審查代理驗收**（第 6 步）→ **寄 ready**（第 7 步）。寄之前程式自己查關卡：驗收機綠、外部審查完成（最新的 commit 審過、沒有 P0）、每條意見有回覆、不採納的都合規矩；本機 `tests.txt` 一定要在（全套測試的輸出，要有「Ran N tests」那一行），條數要等於驗收機的、最後是全綠沒有 skipped——缺檔或讀不出條數都寄不出去。檔案內容裡的具名字串由程式自己帶鹽掃，不看 `tests.txt`：推送之前掃每一筆要推的 commit 改到的檔，寄 ready 之前再掃這個階段改到的每一個檔。命中就把檔改掉（中間的 commit 裡有的話，那幾筆要重做，不可以強推蓋掉已經推上去的——那種情況寄 `--kind stop`）；缺鹽就寄 `--kind stop`，信裡寫明鹽不在。ready 的信裡程式會多列一段「這個階段刪了、改了哪些既有的測試與突變」（驗收機算的，名稱逐條）：每一條為什麼刪、為什麼改，要先寫進 PR 內文與 CHANGELOG，David 是看著那一段放行的。

## 外部審查未完成（逾時、額度用完、設定被關）

`review-status --wait` 逾時、或寄 ready 時程式說「外部審查還沒完成」：程式會記下「外部審查未完成」。你寄 `--kind stop`（等級會是「要你決定」），信裡給 David 兩個選項：

- **等**：Codex 審完或額度恢復之後，他手打「繼續 <階段>」，你回到第 3 步。
- **免除**：他手打「免外部審查 <階段>」（一次性、綁現在的 commit、24 小時內有效；有新 commit 或「修改」就作廢）。免除只免外部審查：驗收機要綠、審查代理要批准、合併仍要「放行 <階段>」。信、CHANGELOG 與回滾表那一列都要寫「本階段未經外部審查（David 親手免除）」。

這一段改到保護系統本身（第一層或自動駕駛自己的檔）時，信裡要寫明這一點，並說等 Codex 比較妥當。

## 重大意見被判「不採納」

P0 不能不採納，一定要修。P1 要不採納，理由欄只能寫「Cowork 裁決：<檔名>」或「延後到 <階段>，Cowork 裁決：<檔名>」。<檔名>只認 hook 存的那一份（報告資料夾裡的 `裁決-<時間>.md`）：David 第一行手打「裁決：<階段>」、下面貼 Cowork 寫的內容，而且內容裡要寫出那一條的留言編號。你自己放進資料夾的檔不算、改過 hook 存的檔也不算（程式比內容雜湊）。沒有這樣的裁決程式當成還沒回覆，ready 寄不出去：寄 `--kind stop`（要你決定），把那幾條意見與你的理由寫進信，由 David 與 Cowork 裁決。延後的寫進 `docs/AUTOPILOT.md` 的「待辦」。

## 驗收機本身有改（只會發生在一般模式的階段）

程式用 git 比對這個 commit 上的 `verify.yml`、`scripts/verify_ci.py`、`scripts/mutations/run_mutations.py`、`known_survivors.json` 跟 main 上的內容；不一樣，驗收機的綠就不算，`verify`／寄 ready 都會說「驗收機本身有改」並記下來。這時寄 `--kind stop`（信裡程式會附完整的 diff），請 David 看過後在一般模式手打「驗收機變更 <階段>」（一次性、綁這個 commit；有新 commit 或「修改」就作廢）。它只讓這個 commit 可以用驗收機的綠；外部審查、審查代理、放行照舊。只認 push 觸發、流程檔路徑是 `.github/workflows/verify.yml` 的那一次執行；手動觸發的不算。

## 分支上出現不是你推的 commit

推送前的檢查會擋（遠端的頭不是你推過的）、自動駕駛中立刻暫停並寄「要你決定」。不要覆蓋、不要合併它；寫停止報告說明是哪一個 commit，等 David 看過 PR 之後「繼續」。

## 放行之後

照 [merge-steps.md](merge-steps.md)。放行時與合併前程式各再查一次驗收機（查不到就擋）；合併一律在本機 `--no-ff`，推上 main 之後 PR 會自動變成已合併，**不用 GitHub 網頁上的合併按鈕**。
