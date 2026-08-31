<#
    InvestWatch — 家用電腦補抓腳本
    ================================

    為什麼需要這支？
      臺灣銀行的網站會擋掉 GitHub Actions 的資料中心 IP，所以雲端排程抓不到
      黃金存摺、實體金條塊、美元/台幣、人民幣/台幣這 5 項。但同一支程式在
      你自己的電腦上跑就沒問題，所以由這台電腦負責補上那幾項。

    它會做什麼？
      1. 先把 GitHub 上最新的內容拉下來（雲端每天會自己 commit）
      2. 跑 fetch_data.py 抓全部 12 項
      3. 如果 data/ 有變動就 commit 並推回 GitHub
      4. 全程寫進 scripts/update_local.log

    安全性：
      * 只有在工作區乾淨時才會自動快轉合併，不會蓋掉你正在改的東西
      * 推送遇到衝突時用 reset --soft 重新提交，不對 JSON 做逐行合併
      * 任何一步失敗都只是寫進 log，不會跳視窗打擾你

    手動執行：
      powershell -ExecutionPolicy Bypass -File D:\Claude_use\invest-watch\scripts\update_local.ps1
#>

param(
    # morning / midday / close / manual；不給就讓 Python 依台北時間自己判斷
    [string]$Slot = "",

    # 輕量更新：只更新現價，不重抓一年份歷史、不重產報告。
    # 給盤中每半小時的密集更新用（請求數 10 次、約 15 秒，完整版是 14 次、25 秒）。
    [switch]$Light
)

$ErrorActionPreference = "Continue"

# Windows 主控台預設是 Big5(cp950)，不改的話 Python 吐出來的中文會變亂碼
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
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

$dirty = git status --porcelain
if ([string]::IsNullOrWhiteSpace($dirty)) {
    # 工作區乾淨才快轉；不用 merge，避免對產生出來的 JSON 逐行合併
    git merge --ff-only origin/main 2>&1 | ForEach-Object { Write-Log "  git merge: $_" }
} else {
    Write-Log "  工作區有未提交的變動，跳過自動合併（不動你正在改的東西）"
}

# --- 2. 抓資料 -----------------------------------------------------------
$args = @("scripts\fetch_data.py")
if ($Slot -ne "") { $args += @("--slot", $Slot) }
if ($Light)       { $args += "--light" }

Write-Log "執行：python $($args -join ' ')"
$output = & $Python $args 2>&1
$output | ForEach-Object { Write-Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Write-Log "抓取腳本回傳非零結束碼 $LASTEXITCODE —— 仍會嘗試提交已寫入的資料"
}

# --- 3. 有變動就提交並推上去 ---------------------------------------------
git add data/ 2>&1 | Out-Null
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Log "資料沒有變動，這次不用 commit。"
    Write-Log "==================== 結束 ===================="
    exit 0
}

$date = Get-Date -Format "yyyy-MM-dd"
$time = Get-Date -Format "HH:mm"
$msg  = if ($Light) { "data: $date $time 盤中輕量更新（黃金與匯率現價）" }
        else        { "data: $date 本機補抓（含台銀黃金與匯率）" }
git commit -m $msg 2>&1 | ForEach-Object { Write-Log "  git commit: $_" }

for ($i = 1; $i -le 3; $i++) {
    git push origin main 2>&1 | ForEach-Object { Write-Log "  git push: $_" }
    if ($LASTEXITCODE -eq 0) {
        Write-Log "推送成功。"
        Write-Log "==================== 結束 ===================="
        exit 0
    }
    Write-Log "推送失敗（第 $i 次），遠端可能有新的 commit，改以這次抓到的資料重新提交"
    git fetch origin main 2>&1 | Out-Null
    git reset --soft origin/main 2>&1 | Out-Null
    git add data/ 2>&1 | Out-Null
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Log "和遠端內容一樣，不用再 commit。"
        Write-Log "==================== 結束 ===================="
        exit 0
    }
    git commit -m $msg 2>&1 | Out-Null
}

Write-Log "連續三次推不上去，這次先跳過（下一個時段會再試）。"
Write-Log "==================== 結束 ===================="
exit 1
