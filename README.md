# 🎙️ 錄音檔摘要機器人（完全免費版）

**費用：$0 — 完全不花錢。**

自動將 Google Drive 中的 `.mp3` / `.m4a` 錄音檔，轉成 **Notion 重點摘要頁面**，每天在 GitHub Actions 上自動執行。

## 使用的免費服務

| 功能 | 服務 | 費用 |
|------|------|------|
| 語音轉文字 | faster-whisper（本地執行） | $0 |
| AI 摘要生成 | Groq API（Llama 3.3 70B） | $0 |
| 音檔來源 | Google Drive API | $0 |
| 摘要儲存 | Notion API | $0 |
| 自動排程 | GitHub Actions | $0 |
| Markdown 備份 | GitHub repo | $0 |

---

## 運作流程

```
Google Drive（每天掃描）
    ↓ 下載新音檔
faster-whisper（本地語音轉文字，免費）
    ↓ 逐字稿
Groq API / Llama 3.3 70B（生成重點摘要，免費）
    ↓
Notion「錄音檔摘要」資料庫（每檔建立一頁）
    ↓（選填）
GitHub repo（Markdown 備份 + 自動更新索引）
```

---

## 設定步驟（共 4 個必填 + 1 個 GitHub 上傳）

### 第一步：申請 Groq 免費 API 金鑰

1. 前往 [console.groq.com](https://console.groq.com/keys)
2. 用 Google 帳號或 Email 註冊（不需要信用卡）
3. 點「Create API Key」，複製金鑰（`gsk_...` 開頭）

> 💡 Groq 免費額度每天超過 100 萬 tokens，一般使用完全夠用。

---

### 第二步：設定 Google Drive 服務帳號

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立新專案（或使用現有專案）
3. 進入 **API 和服務** → **啟用 API** → 搜尋並啟用 **Google Drive API**
4. 進入 **API 和服務** → **憑證** → **建立憑證** → 選 **服務帳號**
5. 填入名稱後建立，下載 **JSON 金鑰檔**，存為 `service_account.json`
6. 開啟 Google Drive，在要掃描的資料夾上點右鍵 → **共用** → 輸入服務帳號 email（`xxx@xxx.iam.gserviceaccount.com`），設為「檢視者」

> 💡 資料夾 ID 從 URL 取得：`https://drive.google.com/drive/folders/【這段就是ID】`

---

### 第三步：建立 Notion Integration 與資料庫

**取得 Notion Token：**
1. 前往 [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. 點「+ 新增整合」→ 填入名稱 → 選擇 Workspace
3. 複製 **Internal Integration Token**（`secret_...` 開頭）

**建立「錄音檔摘要」資料庫：**
1. 在 Notion 建立新頁面，類型選**資料庫（Table Database）**
2. 命名為「錄音檔摘要」
3. 在頁面右上角「…」→ **Connections** → 連接你的 Integration
4. 加入以下欄位：

| 欄位名稱 | 類型 |
|---------|------|
| Name（預設已有） | Title |
| 檔案名稱 | Text |
| 資料夾 | Text |
| 處理日期 | Date |
| 時長（分鐘） | Number |
| 關鍵字 | Multi-select |

5. 資料庫 ID 從 URL 複製：`https://www.notion.so/【這32碼就是ID】?v=...`

---

### 第四步：建立 GitHub repo 並上傳程式碼

1. 在 [github.com](https://github.com) 建立新 repo（建議設為**公開 Public**，這樣 GitHub Actions 免費分鐘數無限制）
2. 將以下檔案上傳到 repo：
   ```
   audio_summary_bot.py
   requirements.txt
   .env.example
   .gitignore
   .github/workflows/schedule.yml
   ```
   > ⚠️ **不要上傳** `.env` 和 `service_account.json`（`.gitignore` 已排除）

3. 進入 repo **Settings → Secrets and variables → Actions**，加入以下 **Secrets**：

| Secret 名稱 | 說明 | 取得方式 |
|-------------|------|---------|
| `GROQ_API_KEY` | Groq 免費 API 金鑰 | 第一步 |
| `NOTION_TOKEN` | Notion Integration Token | 第三步 |
| `NOTION_DATABASE_ID` | Notion 資料庫 ID | 第三步 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **整個 JSON 檔的內容**（複製貼上） | 第二步 |
| `GOOGLE_DRIVE_FOLDER_ID` | 要掃描的資料夾 ID（或留空掃全部） | 第二步 |
| `SUMMARY_GITHUB_TOKEN`（選填） | GitHub Personal Access Token | [settings/tokens](https://github.com/settings/tokens) |

完成後，進入 repo **Actions** 頁面，就會看到「🎙️ 錄音檔摘要機器人」工作流程，每天早上 9 點（台北時間）自動執行。

---

## 手動執行測試

在 GitHub Actions 頁面，點「Run workflow」可以手動觸發，也可以選擇強制重新處理所有檔案。

---

## Whisper 模型說明

| 模型 | 大小 | 速度 | 中文準確度 | 建議用途 |
|------|------|------|----------|---------|
| tiny | 75MB | 最快 | 普通 | 測試用 |
| base | 150MB | 快 | 普通 | 短錄音 |
| **small** | 250MB | 中等 | **良好** | **GitHub Actions（預設）** |
| medium | 800MB | 較慢 | 優秀 | 本機執行 |
| large-v3 | 1.5GB | 慢 | 最佳 | 本機 GPU |

---

## Notion 摘要頁面內容

每個錄音檔自動建立的 Notion 頁面包含：

```
🎙️ [錄音檔名稱] — 2026/05/26
──────────────────────────────
📁 資料夾：會議錄音　｜　⏱ 時長：45:23　｜　📅 2026-05-26 09:00

## 📋 重點摘要
- 討論 Q3 產品路線圖，確認三個核心功能優先順序
- 決定上線時程為 8 月底，前後端需同步配合
- 行銷活動將配合產品上線，由 Jessica 負責規劃

## 🕐 時間軸
- 00:00 開場與上次回顧
- 08:30 產品功能討論
- 25:00 時程規劃
- 38:00 行動事項確認

## 💡 行動事項
1. 完成原型設計（Alex，截止 6/15）
2. 行銷企劃書（Jessica，截止 6/20）

## 🏷️ 關鍵字標籤
產品規劃, Q3, 上線時程, 行銷活動, 功能優先級

📝 完整逐字稿（點擊展開）
```

---

## 🤖 LINE 推薦機器人（`line_bot/`）

這是**另一支獨立的程式**，跟上面那支跑在 GitHub Actions 的摘要機器人分開部署。
它讀取 Notion 上的摘要資料庫，讓夥伴在 LINE 裡用自然語言找錄音檔。

部署方式：Render（或任何 PaaS）指向 `line_bot/`，用 `Procfile` 啟動。

### 環境變數

完整範本見 [`line_bot/.env.example`](line_bot/.env.example)。
**這份清單跟根目錄的 `.env.example` 不同，兩者不可混用。**

| 變數 | 必填 | 說明 |
|---|:---:|---|
| `LINE_CHANNEL_SECRET` | ✅ | LINE Developers Console 取得 |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✅ | 同上 |
| `GROQ_API_KEY` | ✅ | 可用逗號分隔多把金鑰，自動輪換 |
| `GROQ_MODEL` | | 預設 `llama-3.3-70b-versatile` |
| `NOTION_TOKEN` | ✅ | |
| `NOTION_DATABASE_ID` | ✅ | 錄音檔摘要資料庫 |
| `NOTION_DATABASE_B_ID` | | 第二個資料庫，管理員「雙硬碟模式」才用到 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | PaaS 上請貼整份 JSON 內容 |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | | 本機開發用的金鑰檔路徑 |
| `BOT_PASSWORD` | | 通關密碼。留空 = 任何人加好友就能用 |
| `SUMMARY_GITHUB_TOKEN` | ⚠️ | **設了 `BOT_PASSWORD` 就必填**，見下方說明 |
| `GITHUB_REPO` | | 授權名單存放的 repo，未設時用本專案 |
| `GITHUB_BRANCH` | | 預設 `main` |

### ⚠️ 關於通關密碼與授權名單

通過密碼的使用者會被記進 `line_bot/authorized_users.json`，
**寫回這個檔案需要 `SUMMARY_GITHUB_TOKEN`（需 repo 寫入權限）**。

沒設 token 的話，授權名單只存在記憶體 —— 服務休眠或重新部署後就消失，
所有夥伴都要重打一次密碼。這是最容易漏掉、也最常被誤判成「機器人壞了」的設定。

另外：**改動 `BOT_PASSWORD` 會讓整份名單作廢**，所有人都需要重新輸入一次新密碼。

### 檢查設定是否正確

部署完成後，用瀏覽器打開服務的 `/health`：

```
https://你的服務名稱.onrender.com/health
```

```jsonc
{
  "status": "ok",
  "auth": {
    "authorized_now": 12,          // 目前記得幾個人
    "github_token_set": true,      // ← false 代表授權會掉，要補 token
    "github_repo": "TSAITZUNG-HUNG/Audio-file-summary",
    "load_source": "github",       // github / tmp / empty
    "load_error": "",
    "last_save": "ok"
  }
}
```

只回傳布林值與計數，不含任何金鑰內容。

---

## 常見問題

**Q：Public repo 的程式碼會被看到，安全嗎？**
A：安全。API 金鑰都放在 GitHub Secrets（加密），程式碼本身不包含任何金鑰，可以公開。

**Q：GitHub Actions 每月免費分鐘數夠用嗎？**
A：公開 repo 免費分鐘數無限制；私有 repo 每月 2,000 分鐘。以 1 小時錄音用 small 模型，約需 20-30 分鐘，一個月處理 60 小時錄音才會達到上限。

**Q：processed_files.json 快取怎麼運作？**
A：每次執行時，機器人會比對已處理的 Google Drive 檔案 ID，自動跳過舊檔案，只處理新上傳的錄音。

**Q：音檔太大（超過 1GB）怎麼辦？**
A：GitHub Actions runner 有 7GB RAM，一般錄音沒問題。超大檔案建議先在本機用 ffmpeg 壓縮：
```bash
ffmpeg -i input.m4a -b:a 64k output.mp3
```

---

## 專案結構

```
audio_summary_bot.py           # 主程式（完全免費版）
requirements.txt               # Python 套件
.env.example                   # 設定範本（複製為 .env 使用）
.env                           # 你的設定（不要上傳！）
service_account.json           # Google 金鑰（不要上傳！）
processed_files.json           # 已處理記錄（自動產生）
.gitignore                     # 排除敏感檔案
.github/
  workflows/
    schedule.yml               # GitHub Actions 排程
    cleanup_notion.yml         # Notion 日期清理排程
audio-summaries/               # 產出的 Markdown 摘要（自動更新）
  index.md                     # 全部摘要的索引

line_bot/                      # 🤖 LINE 推薦機器人（獨立部署）
  app.py                       # 主程式（Flask webhook）
  .env.example                 # LINE Bot 專用設定範本
  authorized_users.json        # 通關密碼授權名單（程式自動寫回）
  ma_manual.json               # 《超連鎖商店手冊》查詢資料
  requirements.txt             # LINE Bot 套件
  Procfile                     # PaaS 啟動指令
```
