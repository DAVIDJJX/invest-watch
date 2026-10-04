# 第三方：驗收機與 Codex（SKILL.md 第 4b 步的細節；P2）

兩個施工方改不了的第三方：**驗收機**（GitHub 的電腦從實際的 commit 重跑全套測試、三種掃描、突變；`.github/workflows/verify.yml`＋`scripts/verify_ci.py`）與**外部審查員 Codex**（OpenAI 的程式碼審查，讀 PR 的實際 diff，只標 P0／P1）。兩者的結果由 hook 讀 GitHub，不由你轉述。

## 順序（本機全套綠、突變紅、commit、標籤、推分支與標籤之後）

1. **等驗收機**：`py -3.12 .claude/hooks/iw_notify.py verify --stage <階段> --wait`（每 60 秒查一次，上限 40 分鐘）。紅就先修（修完是新 commit，重推、重等）；結果檔會抓到 `.autopilot/runs/<階段>/verify/<commit>/verify-result.json`。
2. **開 PR**（只准這一支開）：先寫 `.autopilot/runs/<階段>/pr-body.md`——只寫改了什麼，**不放規格原文**、不放本機路徑、不放任何個人資料——再 `py -3.12 .claude/hooks/iw_notify.py pr open --stage <階段> --title "停點 <階段>：一句話" --body-file .autopilot/runs/<階段>/pr-body.md`。程式先過隱私掃描才送。改標題內文用 `pr edit`。
3. **等 Codex**：`py -3.12 .claude/hooks/iw_notify.py review-status --stage <階段> --wait`（上限 60 分鐘）。Codex 設了 Automatic review 會自己審；10 分鐘沒動靜就 `py -3.12 .claude/hooks/iw_notify.py pr request-review --stage <階段>`（留言內容寫死是 `@codex review`，之後有新 commit 要重審也只用這一句）。
4. **回覆意見**：讀 `review-status` 印出來的 findings，寫 `.autopilot/runs/<階段>/03_第三方審查.md`，固定一張表、一行一條：

       | 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |
       |---|---|---|---|---|
       | 1234567 | P1 | js/app.js:42 | 採納並修 | commit abc1234 |
       | 1234568 | P1 | scripts/x.py:9 | 不採納 | 理由… |

   每一條都要有；「採納並修」就修在標籤之後的 commit、重推、回到第 1 步（驗收機與 Codex 都要再過一次）。只認 Codex 機器人對 PR 最新 commit 的意見；別人的留言忽略（信裡會列出來）。沒標等級的意見當 P1。
   **不在 PR 上回覆、不爭論、不 resolve、不刪、不隱藏、不駁回**——守門會擋，那也不是我們的做法。
5. **審查代理驗收**（第 6 步）→ **寄 ready**（第 7 步）。寄之前程式自己查關卡四件：驗收機綠、外部審查完成、每條意見有回覆、沒有 P0／P1 被判不採納；本機 `tests.txt` 的條數要等於驗收機的。

## 外部審查未完成（逾時、額度用完、設定被關）

`review-status --wait` 逾時、或寄 ready 時程式說「外部審查還沒完成」：程式會記下「外部審查未完成」。你寄 `--kind stop`（等級會是「要你決定」），信裡給 David 兩個選項：

- **等**：Codex 審完或額度恢復之後，他手打「繼續 <階段>」，你回到第 3 步。
- **免除**：他手打「免外部審查 <階段>」（一次性、綁現在的 commit、24 小時內有效；有新 commit 或「修改」就作廢）。免除只免外部審查：驗收機要綠、審查代理要批准、合併仍要「放行 <階段>」。信、CHANGELOG 與回滾表那一列都要寫「本階段未經外部審查（David 親手免除）」。

這一段改到保護系統本身（第一層或自動駕駛自己的檔）時，信裡要寫明這一點，並說等 Codex 比較妥當。

## 重大意見被判「不採納」

P0／P1 判不採納，ready 寄不出去。寄 `--kind stop`（要你決定），把那幾條意見與你不採納的理由寫進信，由 David 與 Cowork 裁決（他們的答覆會以「裁決：<階段>」存下來）。

## 分支上出現不是你推的 commit

推送前的檢查會擋（遠端的頭不是你推過的）、自動駕駛中立刻暫停並寄「要你決定」。不要覆蓋、不要合併它；寫停止報告說明是哪一個 commit，等 David 看過 PR 之後「繼續」。

## 放行之後

照 [merge-steps.md](merge-steps.md)。放行時與合併前程式各再查一次驗收機（查不到就擋）；合併一律在本機 `--no-ff`，推上 main 之後 PR 會自動變成已合併，**不用 GitHub 網頁上的合併按鈕**。
