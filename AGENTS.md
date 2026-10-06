# AGENTS.md — 給 Codex 的說明（InvestWatch）

這個倉庫用 Codex **只做程式碼審查**：讀 pull request 的 diff、指出重大問題。請不要修改程式、不要推分支、不要開 pull request。所有合併都由倉庫的主人在本機完成，不經過 GitHub 網頁的合併按鈕。

請用**繁體中文**寫審查意見。每一條意見附上檔名與行號、為什麼是問題、以及一個可行的做法。只標 P0／P1 這類重大問題；風格、命名、排版一律不提。

專案的背景：這是一個公開的靜態網站（GitHub Pages），用 Python 腳本與 GitHub Actions 定時抓公開行情、算幾個固定規則的指標，前端只有原生 JavaScript。沒有後端、沒有登入、沒有資料庫。使用者自己的資料只存在他的瀏覽器與另一個私人倉庫，公開倉庫裡永遠不能有。

## Code Review Rules

### 請特別檢查這八點

1. **隱私**：個人的持有明細、金額、具名的基金、房屋、工作收入、貸款、曝險百分比，不得出現在任何檔案——包括測試 fixture、註解、commit 訊息、PR 內文、工作流程的輸出。看到任何像真實個人資料的數字或名稱，標 P0。權杖、密碼、本機的絕對路徑、電子郵件地址也一樣。
2. **擋字串**：不得出現擋字串。清單與唯一的例外句在 `scripts/test_analysis_guards.py`，審查時請以該檔為準。例外清單不可以變長，不可以為任何檔開新的豁免。出現了就標 P1。
3. **門檻常數**：同一個門檻（數字、天數、視窗）只能有一個常數。程式、測試、文件各寫一份而沒有測試釘住它們相等，標 P1。
4. **DOM 與請求數**：既有畫面的 DOM 只能增加、不能減少；儀表板的首屏請求數不得增加（上限以 `docs/ANALYSIS.md` 為準）。
5. **測試數與突變數**：不得減少。被改或被刪的既有測試、既有突變（`scripts/mutations/autopilot_mutations.py`），PR 要寫出理由；沒寫就標 P1。新的規則要有測試，也要有「把它改壞、測試會紅」的突變對照。
6. **資料狀態**：「資料不足」（之後會有）和「不適用」（這類標的本來就沒有）不能混用；讀不到檔是第三種狀態。資料不足時不可以硬算，估算要標「估算」。
7. **保護範圍**：動到保護範圍的檔，要在 PR 內文標示（PR 內文最後有一段程式列的清單；沒有那一段、或清單漏了 diff 裡的檔，標 P1）。保護範圍寫在 `.claude/autopilot/config.json` 的 `tier1`、`selfFiles`、`tier2`：排程與工作流程（`.github/workflows/`、`data/schedule.json`、`scripts/update_local.ps1`、`scripts/publish.py`）、資料來源與標的清單（`scripts/net_policy.py`、`data/assets.json`）、隱私保險絲（`.gitignore`、`scripts/sensitive_terms_hmac.json`）、驗收機（`scripts/verify_ci.py`、`scripts/mutations/`、這個 `AGENTS.md`）、保護程式本身（`.claude/`）。
8. **對外請求**：任何對外請求都不得打臺灣銀行（`bot.com.tw`）；可以連的主機只有 `scripts/net_policy.py` 的白名單。測試一律離線，測試裡出現真的連線就標 P1。

### 誠實

- 分析的輸出不做任何加權的總評、不加總、不計數（「幾個面向怎樣」也不行）；每個面向各自表態，附一句理由與證據等級。
- 日期來自資料來源，不是抓取時間；抓不到要大聲失敗，不能悄悄用舊值。
- 頁尾那一句固定聲明要在、一字不差，原文以 `docs/ANALYSIS.md` 為準。

### 架構與排程

- `data/assets.json` 的 owner 與 type 是唯一的真相來源，不能出現第二份標的清單。
- 分析只在雲端 15:30 那一輪完整更新計算；輕量更新不碰分析。
- 不新增排程、不改觸發方式與時刻。`.github/workflows/update-data.yml`、`data/schedule.json`、`scripts/update_local.ps1`、`scripts/publish.py` 一字不改；改了就是 P0。

### 自動駕駛的保護（`.claude/` 與驗收機）

- `.claude/hooks/`、`.claude/autopilot/`、`.claude/settings.json`、`.claude/agents/`、`.claude/skills/` 是保護程式：改它們要經過倉庫主人親手「放行」。這類改動請看三件事：每條新規則有沒有對應的測試、有沒有突變對照、說明文件（`docs/AUTOPILOT.md`）有沒有同步。
- `.github/workflows/verify.yml`、`scripts/verify_ci.py`、`scripts/mutations/run_mutations.py`、`scripts/mutations/known_survivors.json` 是驗收機本身。一般的階段不能動；PR 動到它們而內文沒有說明為什麼，標 P0。
- 任何能讓「合併進 main」「推上 main」「改寫歷史」「跳過 pre-push 檢查」「不經過驗收機或審查就寄出可以合併的通知」繞過保護的寫法，標 P0。

### 流程

- 改動要逐檔 `git add`；CHANGELOG 三段式（改了什麼／怎麼驗的／怎麼退回）；退回指令要寫得出來。
- `docs/` 底下每個 `.md` 有大小上限的守門。
- 測試用的數字一看就要是假的（10／20／30 這一類），不能像任何真實的個人資料。
