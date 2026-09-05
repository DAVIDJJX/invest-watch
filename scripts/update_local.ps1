<#
    InvestWatch — 家用電腦補抓腳本
    ================================

    為什麼需要這支？
      臺灣銀行的網站會擋掉 GitHub Actions 的資料中心 IP，所以雲端排程抓不到
      黃金存摺、實體金條塊、美元/台幣、人民幣/台幣這 5 項。但同一支程式在
      你自己的電腦上跑就沒問題，所以由這台電腦負責補上那幾項。

    它會做什麼？
      1. 先跟 GitHub 對齊（fetch，工作區乾淨就快轉）
      2. 確認自己在 main 上而且沒有落後——不是的話直接中止
      3. 跑 fetch_data.py --source local，只抓本機負責的 5 項
      4. 跑 merge_latest.py，把兩邊的分片合成 data/latest.json
      5. 跑 publish.py，提交並推回 GitHub（推不上去會安全地重試）
      6. 全程寫進 scripts/update_local.log

    本機【不產生報告】：報告一律由雲端的 GitHub Actions 產生。
    兩邊都產的話會互相覆蓋同一份 data/report-latest.json。

    安全性：
      * 不在 main 上就中止，不會自作主張切分支
      * 落後遠端而且快轉不了（例如工作區有未提交的變動擋住）也中止，
        免得拿舊的基準去提交
      * 競態重試在 publish.py 裡，會先把對方剛推上來的檔案取回工作區再重算，
        不會用 reset --soft 把對方的資料還原掉
      * 任何一步失敗都只是寫進 log，不會跳視窗打擾你

    手動執行：
      powershell -ExecutionPolicy Bypass -File D:\Claude_use\invest-watch\scripts\update_local.ps1
#>

param(
    # morning / midday / close / manual；不給就讓 Python 依台北時間自己判斷
    [string]$Slot = "",

    # 輕量更新：只更新現價，不重抓一年份歷史。
    # 給盤中每半小時的密集更新用（請求數 10 次、約 15 秒，完整版是 14 次、25 秒）。
    [switch]$Light,

    # 這台電腦負責哪一邊。本機一律是 local；留這個參數是為了讓錯誤的呼叫
    # （例如手滑打成 all）能被下面的防呆擋下來，而不是靜靜地跑成全抓。
    [string]$Source = "local"
)

$ErrorActionPreference = "Continue"

# Windows 主控台預設是 Big5(cp950)，不改的話 Python 吐出來的中文會變亂碼
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# --- 防呆：本機絕對不可以跑 --source all -----------------------------------
# all 會連雲端負責的 8 項一起抓，並寫出 data/sources/cloud.json，
# 等於本機又去蓋掉雲端的分片——那正是這次改架構要消滅的問題。
# all 只留給手動測試，這支腳本與排程一律拒絕。
if ($Source -ne "local") {
    if ($Source -eq "all") {
        Write-Output "錯誤：update_local.ps1 不接受 -Source all。"
        Write-Output "      all 會寫出 data/sources/cloud.json，等於本機去覆蓋雲端的分片。"
    } else {
        Write-Output "錯誤：-Source 只接受 local（收到的是「$Source」）。"
    }
    Write-Output "      本機請用 -Source local（預設值）；要測 all 請直接跑 python 指令。"
    exit 2
}
$env:PYTHONIOENCODING = "utf-8"

$RepoDir = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $PSScriptRoot "update_local.log"
$Python  = "C:\Users\david\AppData\Local\Programs\Python\Python312\python.exe"

function Write-Log {
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

# 記錄檔太大就砍掉重來（留最後 500 行）
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 300KB)) {
    $tail = Get-Content $LogFile -Tail 500
    Set-Content -Path $LogFile -Value $tail -Encoding utf8
}

Write-Log "==================== 開始 ===================="
Set-Location $RepoDir

if (-not (Test-Path $Python)) {
    Write-Log "找不到 Python：$Python  —— 中止"
    exit 1
}

# --- 1. 先跟 GitHub 對齊 -------------------------------------------------
git fetch origin main 2>&1 | ForEach-Object { Write-Log "  git fetch: $_" }

# --- 2. 分支保險 ---------------------------------------------------------
# 排程是無人看管跑的，所以絕對不能自作主張切分支或推到別的地方。
# 開發時 repo 可能停在某個功能分支上（例如 feat/split-sources），
# 那時本機抓到的資料推不回 main，網站也不會更新；與其安靜地做白工，
# 不如直接中止並在 log 留下原因。
$branch = (git rev-parse --abbrev-ref HEAD | Select-Object -First 1)
if ($branch -ne $null) { $branch = $branch.Trim() }
if ($branch -ne "main") {
    Write-Log "目前在分支「$branch」，不是 main —— 中止，不自作主張切分支。"
    Write-Log "  （開發完成把分支 merge 回 main 之後，排程就會自動恢復正常）"
    Write-Log "==================== 結束 ===================="
    exit 3
}

$dirty = git status --porcelain
if ([string]::IsNullOrWhiteSpace($dirty)) {
    # 工作區乾淨才快轉；不用 merge，避免對產生出來的 JSON 逐行合併
    git merge --ff-only origin/main 2>&1 | ForEach-Object { Write-Log "  git merge: $_" }
} else {
    Write-Log "  工作區有未提交的變動，跳過自動快轉（不動你正在改的東西）"
}

# 快轉之後還是落後，代表有東西擋住（通常是未提交的變動）。
# 這時繼續跑會拿舊的基準去抓、去合併，合出來的 latest.json 會少掉對方
# 已經推上去的新資料，所以停在這裡比較誠實。
$behind = (git rev-list --count HEAD..origin/main | Select-Object -First 1)
if ($behind -ne $null) { $behind = $behind.Trim() }
if ($behind -ne "0") {
    Write-Log "本機落後 origin/main $behind 個 commit 且無法快轉 —— 中止。"
    Write-Log "  請先手動處理未提交的變動（git status 看一下），再讓排程接手。"
    Write-Log "==================== 結束 ===================="
    exit 4
}

# --- 3. 抓資料（只抓本機負責的 5 項）------------------------------------
$pyArgs = @("scripts\fetch_data.py", "--source", $Source)
if ($Slot -ne "") { $pyArgs += @("--slot", $Slot) }
if ($Light)       { $pyArgs += "--light" }

Write-Log "執行：python $($pyArgs -join ' ')"
$output = & $Python $pyArgs 2>&1
$output | ForEach-Object { Write-Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Write-Log "抓取腳本回傳非零結束碼 $LASTEXITCODE —— 仍會嘗試提交已寫入的資料"
}

# --- 4. 合併成 latest.json ----------------------------------------------
# fetch_data.py 現在只寫自己的分片，不寫 latest.json。
# 這一步把本機分片和雲端上次推上來的分片合成前端要看的那一份。
Write-Log "執行：python scripts\merge_latest.py"
$output = & $Python "scripts\merge_latest.py" 2>&1
$output | ForEach-Object { Write-Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Log "合併失敗（結束碼 $LASTEXITCODE）—— 中止，不提交半成品。"
    Write-Log "==================== 結束 ===================="
    exit 5
}

# --- 5. 提交並推上去 ------------------------------------------------------
# 競態重試全部在 publish.py 裡，雲端和本機共用同一份實作。
# --rebuild 只給 merge：本機不產報告，重試時也絕對不要去碰 report.py
# 或 data/report-latest.json，那是雲端的檔案。
$date = Get-Date -Format "yyyy-MM-dd"
$time = Get-Date -Format "HH:mm"
$msg  = if ($Light) { "data: $date $time 盤中輕量更新（本機：黃金與匯率現價）" }
        else        { "data: $date 本機補抓（含台銀黃金與匯率）" }

$pubArgs = @("scripts\publish.py", "--source", "local", "--rebuild", "merge", "--message", $msg)
Write-Log "執行：python $($pubArgs -join ' ')"
$output = & $Python $pubArgs 2>&1
$output | ForEach-Object { Write-Log "  $_" }
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Log "完成。"
} else {
    Write-Log "提交/推送未成功（結束碼 $rc），下一個時段會再試。"
}
Write-Log "==================== 結束 ===================="
exit $rc
