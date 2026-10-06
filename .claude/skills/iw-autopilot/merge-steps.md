# 放行之後：合併與收尾（SKILL.md 第 8 步的細節）

hook 說「已放行 <階段>：通行證已開」之後才做。通行證只准合併它指定的那一個 commit、一次；合併後再推一筆只改 `README.md` 與 `docs/CHANGELOG.md` 的文件 commit，一次。

## 事先備好（放行之前就做）

- 文件那一筆的內容寫成腳本（放 `.autopilot/runs/<階段>/`，吃合併 commit 編號當參數，改 README 的回滾表與進度、CHANGELOG 的合併紀錄）。放行後只要執行它、commit、推——合併推上去之後到文件推上去之間不做別的事。
- 退回指令要寫對：保護檔還在的合併（P1 之後的每一個），David 從終端機退回要用 `git revert -m 1 <合併> && git push --no-verify`（第二道對沒有通行證的 main 推送對誰都擋；`--no-verify` 是給他的，你用會被擋）。

## 合併（固定分兩個指令，不要合在一段）

1. `git fetch origin`
2. **第一個指令只建 worktree**：`git worktree add --detach .claude/worktrees/stop<階段>-merge origin/main`；接著 `ls .claude/worktrees/stop<階段>-merge` 確認在了。
3. **第二個指令才進去合併**：`cd .claude/worktrees/stop<階段>-merge && git merge --no-ff --no-commit feat/stop<階段>`（或通行證指定的那個 commit）。
4. 在合併後的樹上跑全套測試（寫死的那一個指令）→ `git commit`（訊息寫成檔用 `-F`）。
5. 推之前再 `git fetch origin`：`origin/main` 還是合併的第一個 parent 才推；不是就 `git checkout --detach origin/main` 以新的為底重做第 3、4 步（不強推）。
6. `git push origin HEAD:main`。推送前的檢查會印「David 放行的合併」。這一次推送，程式會把整條分支每一筆 commit 的訊息與改到的檔都掃一遍（具名字串要帶鹽算），大的階段要幾分鐘：指令的逾時設成 10 分鐘。逾時不是被擋，重下同一個指令。

## 文件那一筆（合併推上去之後立刻做）

7. 執行事先備好的腳本（帶合併 commit 編號）→ `git add -- README.md docs/CHANGELOG.md` → `git commit -F …` → `git push origin HEAD:main`。只改這兩個檔、只一筆、合併後 60 分鐘內。
8. 主目錄 `git merge --ff-only origin/main`。

## 收尾

9. `py -3.12 .claude/hooks/iw_notify.py status`：確認狀態是 done、`protectionVersion` 是現在的版本；改了保護檔的階段再跑 `py -3.12 scripts/autopilot_install.py --check`。
10. 收 worktree（`git worktree remove …`）與分支（本機 `git branch -d`、遠端 `git push origin --delete`）；標籤不動；停預覽伺服器；確認線上。
11. 文件併進這一筆的早先階段（例如 P1 的回滾表併進 P1-1）：`py -3.12 .claude/hooks/iw_notify.py finish-docs --stage <早先的階段> --merged-into <階段>`。
12. `py -3.12 .claude/hooks/iw_notify.py close --stage <階段>`；最後回報合併 commit 與退回指令。收尾期間被擋＝一般暫停，等 David 手打「繼續」。

## 中途停了、或時間窗過了

狀態維持「已合併、等補文件」，「繼續」「修改」在這個狀態都沒有作用。寄停止報告（等級會是「要你放行上線」），「要繼續」那一行寫 `放行 <階段>`。David 手打之後 hook 重開一個一次性的時間窗（綁同一個階段與合併 commit），回到第 7 步。
