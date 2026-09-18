<#
    InvestWatch — 家用電腦補抓腳本
    ================================

    為什麼需要這支？
      臺灣銀行的網站會擋掉 GitHub Actions 的資料中心 IP，所以雲端排程抓不到
      黃金存摺（台幣、人民幣）與實體金條塊這 3 項。但同一支程式在
      你自己的電腦上跑就沒問題，所以由這台電腦負責補上那幾項。
      （匯率原本也在這裡，2026-09-18 起改由雲端經 FinMind 抓，見 docs/CHANGELOG.md 8-0）

    它會做什麼？
      1. 先跟 GitHub 對齊（fetch，工作區乾淨就快轉）
      2. 確認自己在 main 上而且沒有落後——不是的話直接中止
      3. 跑 fetch_data.py --source local，只抓本機負責的標的
      4. 跑 merge_latest.py，把兩邊的分片合成 data/latest.json
      5. 跑 publish.py，提交並推回 GitHub（推不上去會安全地重試）
      6. 全程寫進 scripts/update_local.log

    本機【不產生報告】：報告一律由雲端的 GitHub Actions 產生。
    兩邊都產的話會互相覆蓋同一份 data/report-latest.json。

    安全性：
      * 不在 main 上就中止，不會自作主張切分支
      * 同一時間只跑一個：發現另一個實例正在跑，寫一行「另一個實例執行中，略過」就以結束碼 0 離開
        （筆電一醒，錯過的幾個工作會同一秒一起補跑，以前會互撞 git）
      * 落後遠端而且快轉不了（已追蹤的檔案有未提交的變動擋住）也中止，
        免得拿舊的基準去提交。未追蹤的檔案不算——不相干的檔案不該讓整條管線停下來
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
# all 會連雲端負責的標的一起抓，並寫出 data/sources/cloud.json，
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
    # 為什麼不用 Add-Content？多個實例同時寫同一個檔案時，它不會報錯，而是各自從「當時的檔尾」
    # 寫下去——後寫的把先寫的蓋掉，那幾行就無聲無息地不見了（2026-09-14 22:23 四個工作同一秒啟動，
    # log 只留下一行「開始」；用「開始」的行數去算啟動次數因此會低估）。
    # 這裡改成【獨占】開檔再附加：同一時間只有一個實例寫得進去，別人開不了檔就等一下再試。
    # 試 40 次（最多約 3 秒）還不行才放棄這一行——log 掉一行不值得讓整支腳本停下來。
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($line + "`r`n")
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $fs = [System.IO.File]::Open($LogFile, [System.IO.FileMode]::Append,
                                         [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
            break
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds (20 + (Get-Random -Maximum 80))
        }
    }
}

# --- 互斥鎖：同一時間只准一個實例動這個倉庫 --------------------------------
# 為什麼需要？四個 Windows 工作各自設了「錯過就補跑」，而「不要同時跑第二個」
# 那個設定只管【同一個工作】。筆電一醒，錯過的三、四個工作會在同一秒一起啟動，
# 對同一個 git 倉庫同時 fetch／merge／commit／push：
#   「Unable to create '.git/index.lock': File exists」
#   「remote rejected … cannot lock ref」
# log 裡最早 2026-09-01 就有，09-13 那次還把一個 0 bytes 的分片推了上去。
# 同時跑的那幾個實例做的是一模一樣的事，留一個就夠了；其餘的寫一行 log 就走，
# 而且用結束碼 0——那不是失敗，工作排程器不需要把它記成錯誤。
#
# 用具名 Mutex 而不是 lock 檔：持有鎖的程序不管怎麼死（被工作排程器的時間上限砍掉、
# 電腦睡著時被終止），Windows 都會自己把鎖收回去，不會留下一個要人手動刪的死檔。
# 鎖的名字帶倉庫路徑的雜湊：同一台電腦上的另一份 clone（例如測試用的沙盒）不會跟正式的互相擋。
# Global\ 讓「工作排程器啟動的」和「你手動在視窗裡跑的」也互相看得到。
$md5 = [System.Security.Cryptography.MD5]::Create()
$repoKey = -join ($md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($RepoDir.ToLowerInvariant())) |
                  ForEach-Object { $_.ToString("x2") })
$MutexName = "Global\InvestWatch-update_local-" + $repoKey.Substring(0, 12)
$script:Mutex = New-Object System.Threading.Mutex($false, $MutexName)
$script:HasLock = $false
try {
    $script:HasLock = $script:Mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # 上一個持有鎖的實例沒放鎖就死了。這種情況 Windows 會把鎖交給我們、同時丟這個例外：
    # 鎖已經是我們的了。不接住的話，之後每一次排程都會死在這裡。
    $script:HasLock = $true
}
if (-not $script:HasLock) {
    Write-Log "另一個實例執行中，略過"
    $script:Mutex.Dispose()
    exit 0
}

function Exit-Script {
    # 拿到鎖之後的每一個出口都走這裡：先放鎖再結束。
    # （就算漏放，程序結束時 Windows 也會收回；這裡只是不想讓下一個實例多走一次「被遺棄的鎖」那條路）
    param([int]$Code)
    if ($script:HasLock) {
        try { $script:Mutex.ReleaseMutex() } catch { }
    }
    $script:Mutex.Dispose()
    exit $Code
}

# 記錄檔太大就砍掉重來（留最後 500 行）——放在鎖後面：這是「讀整個檔再整個寫回去」，
# 兩個實例同時做會把對方剛寫的行砍掉
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 300KB)) {
    $tail = Get-Content $LogFile -Tail 500
    Set-Content -Path $LogFile -Value $tail -Encoding utf8
}

Write-Log "==================== 開始 ===================="
Set-Location $RepoDir

if (-not (Test-Path $Python)) {
    Write-Log "找不到 Python：$Python  —— 中止"
    Exit-Script 1
}

# --- 0. 死掉的 .git\index.lock -------------------------------------------
# git 每次改索引都會先建這個檔、做完就刪。如果 git 做到一半被砍掉（電腦睡著、
# 工作排程器的時間上限），檔案會留著，之後【每一個】git 指令都會失敗，直到有人手動刪掉。
# 這個檔本身不記是誰建的，所以判斷要保守，三個條件都成立才清：
#   我們拿到了互斥鎖（沒有別的實例在跑）＋ 現在沒有任何 git 程序 ＋ 它已經放超過 2 分鐘。
# 任何一個不成立就不動它——你可能正好在編輯器或終端機裡對這個倉庫下 git 指令。
$gitDir = (git rev-parse --git-dir 2>$null | Select-Object -First 1)
if ($gitDir) {
    if (-not [System.IO.Path]::IsPathRooted($gitDir)) { $gitDir = Join-Path $RepoDir $gitDir }
    $indexLock = Join-Path $gitDir "index.lock"
    if (Test-Path $indexLock) {
        $age = [int]((Get-Date) - (Get-Item $indexLock).LastWriteTime).TotalSeconds
        $gitProcs = @(Get-Process -Name git -ErrorAction SilentlyContinue).Count
        if ($gitProcs -eq 0 -and $age -gt 120) {
            Remove-Item $indexLock -Force
            Write-Log "  發現死掉的 index.lock（$age 秒前留下的，現在沒有任何 git 在跑）—— 已清掉"
        } else {
            Write-Log "  index.lock 存在，但不確定是不是死的（$age 秒前、git 程序 $gitProcs 個）—— 不動它"
        }
    }
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
    Exit-Script 3
}

# 只看【已追蹤】的檔案有沒有被改過（--untracked-files=no）。
# 未追蹤的檔案不會被快轉動到，也不會被 publish.py 提交（它只 add 自己清單裡的路徑），
# 沒有理由因為它們而不同步。2026-09-13 有一個不相干的資料夾落在倉庫根目錄，當時這裡把它也算成
# 「工作區不乾淨」，結果本機排程從 09-14 到 09-18 每一次都在下面那個結束碼 4 中止。
$dirty = git status --porcelain --untracked-files=no
if ([string]::IsNullOrWhiteSpace($dirty)) {
    # 工作區乾淨才快轉；不用 merge，避免對產生出來的 JSON 逐行合併
    git merge --ff-only origin/main 2>&1 | ForEach-Object { Write-Log "  git merge: $_" }
} else {
    Write-Log "  已追蹤的檔案有未提交的變動，跳過自動快轉（不動你正在改的東西）"
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
    Exit-Script 4
}

# --- 3. 抓資料（只抓本機負責的標的）------------------------------------
# 本機一律 --slot light：本機不產報告，沒有資格宣告「這是晨報／收盤」。
# 以前 Windows 排程晚上補跑 13:05 的工作，就把整份 latest.json 標成了「午盤」。
# 時段標籤只由產報告的那一方（雲端）決定；Task Scheduler 傳來的 -Slot 只寫 log、不採用。
# mode 不受影響：三個完整更新工作照樣 full（不帶 -Light），UpdateLight 照樣 light。
if ($Slot -ne "") { Write-Log "  忽略 -Slot $Slot：本機一律用 light（時段由雲端決定）" }
$pyArgs = @("scripts\fetch_data.py", "--source", $Source, "--slot", "light")
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
    Exit-Script 5
}

# --- 5. 提交並推上去 ------------------------------------------------------
# 競態重試全部在 publish.py 裡，雲端和本機共用同一份實作。
# --rebuild 只給 merge：本機不產報告，重試時也絕對不要去碰 report.py
# 或 data/report-latest.json，那是雲端的檔案。
$date = Get-Date -Format "yyyy-MM-dd"
$time = Get-Date -Format "HH:mm"
$msg  = if ($Light) { "data: $date $time 盤中輕量更新（本機：台銀黃金現價）" }
        else        { "data: $date 本機補抓（台銀黃金）" }

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
Exit-Script $rc
