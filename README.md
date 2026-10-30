# 東華運動場地借用系統 — 搶場小工具

一句話：**自動讀取可借場地時段 → 排出你要的兩張申請單 → 登入後自動填驗證碼送出。**

給誰用：學生個人工具（只用你自己的帳號）。目標系統是國立東華大學「運動場地借用系統」
（`https://sys.ndhu.edu.tw/gc/sportcenter/SportsFields/`）。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `court_booking.py` | 主程式（所有功能都在裡面） |
| `requirements.txt` | 需要的 Python 套件 |
| `README.md` | 本文件 |
| `captcha_sample_1/2.png` | 驗證碼自我測試產生的範例圖 |
| `.venv/` | 虛擬環境（不上傳 git，見 `.gitignore`） |
| `debug_outline.txt` | `--book` 執行時寫的頁面結構紀錄（不上傳 git，給除錯用） |

## 怎麼裝

已經裝好了（`.venv`）。要重裝的話：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 怎麼跑

```powershell
# 自我檢查：抓登入頁驗證 token/cookie ＋ 抓時段表（不需帳號）
.\.venv\Scripts\python.exe court_booking.py --check

# 本週各場地可借總覽／指定場地的每日時段
.\.venv\Scripts\python.exe court_booking.py
.\.venv\Scripts\python.exe court_booking.py --field VOL0H

# 搶場計畫：依規則排出本週要搶的兩張申請單（不需帳號）
.\.venv\Scripts\python.exe court_booking.py --plan

# 查「某日期要在哪一天的 00:00 跑」（＋12 天規則）
.\.venv\Scripts\python.exe court_booking.py --when 2026/11/12

# 試跑：走完整流程但不送出，把登入後的頁面結構寫進 debug_outline.txt
.\.venv\Scripts\python.exe court_booking.py --book --dry-run

# 正式搶場（帳號密碼執行時輸入，不會寫進任何檔案）
.\.venv\Scripts\python.exe court_booking.py --book

# 等到隔天 00:00（開放新一批日期的時間）再搶
.\.venv\Scripts\python.exe court_booking.py --book --wait-open

# 驗證碼離線自我測試（產生兩張圖各自辨識）
.\.venv\Scripts\python.exe court_booking.py --test-captcha
```

## 小隊長指定的規則（寫在 `court_booking.py` 頂端常數區）

- 類型只選**排球**（`TARGET_KIND_VALUE = "2"`）
- 只搶男 H、男 F 排球場：`VOL0H`、`VOL0F`（`PREFER_FIELDS`，可用 `--fields` 改順序）
- 一週兩張申請單：**星期一或二擇一天 ＋ 星期四或五擇一天**（`DAY_GROUPS`）
- 都是 **20:00–22:00**（`TARGET_HOURS = ["20", "21"]`，剛好兩小時）
- **一張申請＝一個場地＋一天＋兩小時**，不貪多
- 每天 **00:00 開放「當天＋12 天」的日期**（例：10/29 00:00 開放 11/10、10/30 00:00 開放 11/11）；
  整個可借窗＝今天＋6 ～ 今天＋12 這七天。查某天用 `--when 日期`，當天提早跑用 `--wait-open`
- **每組只搶一張**：成功搶到後會寫進本機 `booked_state.json`（只有日期／星期／場地代碼，無個資），
  下次跑會自動跳過已搶的那組，不會超搶成三張

### 搶場時刻表（想搶這幾天，就要在前面那天的 00:00 跑）

| 想搶的日期 | 星期 | 要在幾點跑 |
| --- | --- | --- |
| 2026/11/12 | 四 | **10/31 00:00** |
| 2026/11/13 | 五 | **11/01 00:00** |
| 2026/11/16 | 一 | **11/04 00:00** |
| 2026/11/17 | 二 | **11/05 00:00** |

## 流程五步（`--book` 會照這個走）

1. 抓登入頁，處理 `__VIEWSTATE` 等 token 與 cookie
2. 輸入帳號密碼登入（密碼用 `getpass`，不顯示也不落檔）
3. 找到「新增申請」按鈕並按下
4. 下載驗證碼 → 辨識 → 自動填入 → 送出；**每次都用當下新圖**（驗證碼每次都不一樣），答錯最多重試 **5 次**就停
5. 依計畫完成兩張申請單，印出總結

## 實測紀錄（2026-10-29）

- `--check`：通過（登入頁 7 個隱藏欄位、時段表 51 場地 × 7 天 × 18 時段列）
- `--test-captcha`：通過（兩張合成測試圖 `EA1XH`、`A92SH` 各自獨立辨識，全對）
- `--plan`：通過。**本週（11/04–11/10）週一/二/四/五的 20:00–22:00 在 VOL0H/VOL0F 都已被借走**，程式誠實回報「本週無位」，並提示等 00:00 再搶
- `--when`：通過（11/12→10/31、11/16→11/04、窗內日期→現在就可搶、過期日期→來不及，四種情況都對）
- 「每組只搶一張」記憶：通過（假資料實測：已搶過星期一組會被跳過、兩組都搶過就不排）
- `--book --dry-run`：**待小隊長實測**（需要真實帳號；這一步會把登入後的頁面結構寫進 `debug_outline.txt`）

## 已知限制

1. 登入後「新增申請 → 驗證碼」的頁面結構還沒用真實帳號看過，選擇器可能需要對一次焦；
   先跑 `--book --dry-run`，把 `debug_outline.txt` 給領隊即可對準。
2. 驗證碼辨識率以真實圖片為準；自我測試用的是同類型合成圖。
3. 只搶「當前開放的那一週」；要看/搶下一週，等那天 00:00 之後再跑。

## 紅線（請勿修改）

- 帳號密碼**只在執行時輸入**，絕不寫進任何檔案；本資料夾不放個資。
- 每次對系統的請求**至少間隔 2 秒**；不做平行搶位、不做洪水式重試。
- 驗證碼**最多 5 次**就停；辨識失敗不暴力窮舉。
- 只服務自己的帳號，本工具不代表校方、不繞過任何安全機制之外的東西。
