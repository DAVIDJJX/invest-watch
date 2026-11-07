# 接收模式：收包、測、推、合併（SKILL.md 的接收模式；P3）

David 第一行手打「接收：<階段>」、下面貼那一段的規格（Cowork 放在 `.autopilot/runs/<階段>/00_規格.md` 的同一份）之後才做。程式由雲端（Cowork）寫好打成包，**你不改任何程式**：改檔工具只准寫報告資料夾；git 不准 commit／add／apply／reset／從檔案 fetch；Python 只准寫死的全套測試與主目錄那支 `iw_notify.py`。這些都是守門擋的，被擋＝停止條件，照 SKILL.md 鐵則 3 停下寄信。

模型是設定檔的 receiveModel／receiveEffort（Sonnet 5.5／High）；審查代理照舊。時鐘：從最後一次接受包起 8 小時後只准等包、驗包、套包、寫狀態、寄信、唯讀查看（等包不計時）；從「接收：」或 David 最後一次「繼續」起 7 天是絕對上限。

下面的指令都用主目錄那一支：`py -3.12 .claude/hooks/iw_notify.py …`（寫成 `iw_notify.py`），在主目錄下、路徑相對於主目錄。只有全套測試在 worktree 裡跑；那時寫報告資料夾要用**主目錄的完整路徑**（寫成 `<主目錄>/.autopilot/runs/<階段>/…`）——在 worktree 裡寫相對路徑，會寫進 worktree 自己的 `.autopilot/`，守門會擋。

## 一輪的順序

1. **等包**：`iw_notify.py wait-inbox --stage <階段>`，用背景執行跑（完成會通知你）。逾時（預設 240 分鐘）就再跑一次同一個指令；它會寫 status.json。
2. **驗包**：`iw_notify.py receive --check .autopilot/runs/<階段>/inbox/<NN>`。八條全部由程式判定。不過＝程式寫 `rejected.md`、寫 status.json、寄一封小事的短信、這個號碼算用掉；你**不寄信、不停**，回到第 1 步（修正由雲端做成下一個號碼的包）。
3. **套包**：`iw_notify.py receive --apply .autopilot/runs/<階段>/inbox/<NN>`。只套剛驗過、雜湊沒變的那一包；第一包會自己建 worktree `.claude/worktrees/stop<階段>`（分支 `feat/stop<階段>`）。
4. **本機全套**：在那個 worktree 裡先 `git log -1 --format="commit %H" > <主目錄>/.autopilot/runs/<階段>/tests.txt`，再跑 SKILL.md 寫死的全套指令、輸出接 `>> <主目錄>/.autopilot/runs/<階段>/tests.txt`（兩處都是主目錄的完整路徑）。
5. **跑完的檢查**：`iw_notify.py receive --tested --stage <階段>`。程式核對 tests.txt 的第一行與全綠，並檢查主目錄生效中的保護檔與推送前的檢查沒被動過（雲端寫的測試在筆電上執行）；動到了就暫停、寄「要你決定」。過了才准推。tests.txt 只防「忘了重跑」，測試的證據以驗收機為準。
6. **動到 tier2 的檔**（驗包時會列出來）：寄 `--kind tier2`，停下等 David「繼續」。同一份 diff 看過一次就好（程式比 diff 的指紋）。
7. **推**：在 worktree 裡 `git push origin feat/stop<階段>`；第一次推之後 `git tag stop<階段>`（打在套上的那個 commit）、`git push origin stop<階段>`。之後的包都是標籤之後的 commit，標籤不動。
8. **第三方**：照 pr-steps.md——`verify --stage <階段> --wait`；沒有 PR 就 `pr open`（內文 `pr-body.md` 由你寫在報告資料夾），有就 `pr edit`＋`pr request-review`；`review-status --stage <階段> --wait`。
9. **Codex 有意見**：你不修，等下一個包（回到第 1 步）。那一包附的 `replies.md` 由驗包程式查過格式；你把它的列併進 `.autopilot/runs/<階段>/03_第三方審查.md`（不貼進 PR、不在 PR 上回覆）。
10. **Codex 完成、最新的 commit 沒有 P0**：審查代理驗收（`REVIEW-KIND: acceptance`，規格看「接收：」那一則的本文）→ `send --kind ready`。

status.json 由程式在 verify、review-status、驗包、拒收、套包、暫停、寄信之後自動重寫；你不能直接寫它（守門擋），要補寫就 `iw_notify.py status-write --stage <階段>`。

## 放行之後

照 merge-steps.md。差別只有文件那一筆：**不跑腳本**。CHANGELOG 的三段式雲端已經放在最後一包裡；合併推上去之後，你用改檔工具改合併用 worktree（`stop<階段>-merge`）裡的 `README.md`（回滾表那一列）與 `docs/CHANGELOG.md`（補合併編號），`git add -- README.md docs/CHANGELOG.md`、commit、推。這兩個檔只在「合併推上去之後、文件推上去之前」改得到。

## 不要做的事

- 不改程式、不自己修 Codex 的意見、不重寫被拒的包或 manifest（收件匣只准雲端與程式寫）。
- 不從檔案或別的遠端 fetch、不 merge 包（套包只走 `receive --apply`）。
- 不跑報告資料夾裡的腳本、不開 shell、不用 WebFetch／WebSearch。
