# cron-job.org 設定（外部精準排程 → 觸發 GitHub Actions）

這份文件是給你**照抄**的。每一個 job 的每一個欄位都寫在下面；文件裡**沒有**真的鑰匙，
鑰匙只存在 cron-job.org 的 job 設定裡（以及你自己的密碼管理工具）。

## 為什麼要這樣做

GitHub 自己的 cron 從來不準時（量過：晚 2～5 小時、一天三次只跑兩次），
沒辦法做到「每 30 分鐘更新」和「四個固定時段的報告」。
cron-job.org 是外部的、準時的排程服務，它在指定時間對 GitHub 的 API 發一個請求，
叫 workflow 跑起來，並且**直接告訴 workflow 這一次是什麼**（`mode` 與 `slot`），
workflow 不再自己猜時段。

GitHub 自己的 cron 還留著一行當備援，但只做 light 的資料更新、永遠不產報告——
不管它幾點才觸發，都不會生出一份時間錯的報告。

## 準備好的東西

- GitHub 個人存取權杖（PAT）`invest-watch-dispatcher`：**只需要 Actions 的「Read and write」權限**，範圍限定在 `davidjjx/invest-watch` 這一個倉庫。
- cron-job.org 帳號（免費）。

⚠ PAT 只貼進 cron-job.org 的 job 設定。不要貼進任何檔案、不要貼進聊天、不要 commit。

## 所有 job 共用的部分

| 欄位 | 值 |
|---|---|
| URL | `https://api.github.com/repos/davidjjx/invest-watch/actions/workflows/update-data.yml/dispatches` |
| Request method | `POST` |
| Header 1 | `Authorization` ： `Bearer <貼你的 PAT>` |
| Header 2 | `Accept` ： `application/vnd.github+json` |
| Header 3 | `Content-Type` ： `application/json` |
| 時區 | `Asia/Taipei`（帳號設定裡的 Time zone，或每個 job 的時區都設成這個） |

Request body 依 job 不同，見下表。GitHub 回 **204 No Content** 就是成功（沒有內容是正常的）。

## 六個 job

| # | Job 名稱（自己取，建議照抄） | 排程（台北時間） | Request body |
|---|---|---|---|
| 1 | `invest-watch light 平日` | 週一～週五，08:10 到 17:40，每 30 分（分鐘設 **10** 與 **40**，小時設 **8～17**） | `{"ref":"main","inputs":{"mode":"light","slot":"light"}}` |
| 2 | `invest-watch morning 09:30` | 週一～週五 09:30 | `{"ref":"main","inputs":{"mode":"full","slot":"morning"}}` |
| 3 | `invest-watch midmorning 11:30` | 週一～週五 11:30 | `{"ref":"main","inputs":{"mode":"full","slot":"midmorning"}}` |
| 4 | `invest-watch close 13:35` | 週一～週五 13:35 | `{"ref":"main","inputs":{"mode":"full","slot":"close"}}` |
| 5 | `invest-watch review 15:30` | 週一～週五 15:30 | `{"ref":"main","inputs":{"mode":"full","slot":"review"}}` |
| 6 | `invest-watch light 週末` | 週六、週日，08:10 到 20:10 每 2 小時（小時設 **8,10,12,14,16,18,20**，分鐘設 **10**） | `{"ref":"main","inputs":{"mode":"light","slot":"light"}}` |

`mode` 與 `slot` 的合法組合只有兩種：`light`＋`light`，或 `full`＋四個報告時段／`manual`。
填錯的話 workflow 第一步就會結束並在 Actions 頁顯示紅色錯誤訊息，不會產生錯的資料。

這張表就是 `data/schedule.json` 裡 `cloud` 那一段的來源；改這裡一定要同步改那個檔。

## 點哪裡、打什麼（cron-job.org）

1. 登入 https://cron-job.org → 右上角 **Cronjobs** → **Create cronjob**。
2. **Title**：貼上面的 Job 名稱。
3. **URL**：貼上面那個 `https://api.github.com/.../dispatches`。
4. **Schedule**：選 **Custom**。
   - **Days of week**：平日的 job 勾週一～週五；週末的 job 勾週六、週日。
   - **Hours**：照上表。
   - **Minutes**：照上表。
   - Days of month、Months 保持全選。
   - 確認時區顯示 `Asia/Taipei`；不是的話到右上角帳號 → **Settings** → **Time zone** 改。
5. 切到 **Advanced** 分頁：
   - **Request method**：選 `POST`。
   - **Headers**：按 **Add header** 三次，分別填上面三個 header（Key 與 Value 各一格）。
     `Authorization` 的 Value 是 `Bearer ` 加一個空格再貼 PAT。
   - **Request body**：貼上表對應的 JSON（整行，含大括號）。
6. **Save**（或 **Create**）。
7. 六個 job 都建好之後，回到 Cronjobs 清單，確認六個都是 **Enabled**。

## 怎麼驗證它真的有效

**單一 job（建好當下就做）**

1. 在 Cronjobs 清單找到那個 job，按右邊的 **▶ Execute now**（或「立即執行」）。
2. 幾秒後開 https://github.com/DAVIDJJX/invest-watch/actions
   → 最上面應該出現一筆新的 run，右邊寫著 **workflow_dispatch**（不是 schedule）。
3. 點進去 → 第一個步驟「決定模式與時段（不猜，由觸發者告知）」的輸出要是
   `mode=... slot=...`，跟你 body 裡填的一樣。
4. 往下捲到 **Summary**：會有「dispatch → 開跑延遲 N 秒」和資料表。
5. cron-job.org 那邊：job 的 **History** 分頁應該顯示 **HTTP 204**。
   看到 **401**＝PAT 貼錯或過期；**404**＝URL 打錯（注意大小寫與 `update-data.yml`）；
   **422**＝body 的 JSON 打錯或 `mode`／`slot` 不是合法值。

**兩小時觀察（7-H）**

建好之後在平日盤中觀察 2 小時：Actions 頁每 30 分鐘（:10 與 :40）要各出現一筆
`workflow_dispatch` 的 run，開跑時間跟排定時間差距 **< 1 分鐘**（差距記在 Summary 的
「dispatch → 開跑延遲」；那個延遲是 GitHub 排隊，不是 cron-job.org 遲到）。

**隔天早上（7-I）**

| 時間 | 應該看到 |
|---|---|
| 09:45 前 | `data/archive/<今天>/morning.json` 存在，首頁「最新報告」變成晨報 |
| 11:45 前 | `midmorning.json` |
| 13:50 前 | `close.json` |
| 15:45 前 | `review.json` |

四份都到才算通過。缺哪一份，就去 cron-job.org 看那一個 job 的 History。

## 停用或暫停

- 暫停全部：cron-job.org 清單把六個 job 切成 **Disabled**。GitHub 的備援 cron 每 3 小時
  仍會做 light 更新，所以網站不會完全停；報告會缺席，`freshness` 與之後的 watchdog 會顯示出來。
- 換 PAT：到每個 job 的 Advanced 分頁把 `Authorization` 的值換掉；舊的 PAT 到 GitHub → Settings → Developer settings → Fine-grained tokens 刪掉。
