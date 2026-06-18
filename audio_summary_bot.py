#!/usr/bin/env python3
"""
🎙️ 錄音檔摘要機器人（完全免費版）
=====================================
• 語音轉文字：faster-whisper（本地執行，完全免費）
• AI 摘要生成：Groq 免費 API（Llama 3.3 70B）
• 雲端硬碟：Google Drive（服務帳號，免費）
• 摘要儲存：Notion 頁面（免費）
• 自動排程：GitHub Actions（每天免費執行）

費用：$0 / 完全免費

使用方式：
  1. cp .env.example .env  → 填入 API 金鑰
  2. pip install -r requirements.txt
  3. python audio_summary_bot.py
"""

import os
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 載入 .env ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ════════════════════════════════════════════════════════════════════════════════
# 設定
# ════════════════════════════════════════════════════════════════════════════════

class Config:
    # ── Groq 免費 API（摘要生成）──────────────────────────────────────────────
    # 申請：https://console.groq.com/keys（完全免費）
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL:   str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── Google Drive ────────────────────────────────────────────────────────────
    GOOGLE_SERVICE_ACCOUNT_FILE: str = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
    )
    # 支援多個資料夾 ID，用逗號分隔，例如：
    # GOOGLE_DRIVE_FOLDER_IDS=abc123,def456,ghi789
    # 留空或填 root = 掃描整個雲端硬碟
    GOOGLE_DRIVE_FOLDER_IDS: str = os.environ.get("GOOGLE_DRIVE_FOLDER_IDS", "root")

    @property
    def folder_id_list(self) -> list:
        """回傳資料夾 ID 清單"""
        raw = self.GOOGLE_DRIVE_FOLDER_IDS.strip()
        if not raw or raw.lower() == "root":
            return ["root"]
        return [fid.strip() for fid in raw.split(",") if fid.strip()]

    # ── 雲端硬碟 B（第二個帳號）────────────────────────────────────────────────
    # 多個資料夾 ID 用逗號分隔
    DRIVE_B_FOLDER_IDS: str = os.environ.get("DRIVE_B_FOLDER_IDS", "")
    DRIVE_B_PROCESSED_CACHE: str = os.environ.get("DRIVE_B_PROCESSED_CACHE", "processed_files_b.json")

    @property
    def drive_b_folder_id_list(self) -> list:
        raw = self.DRIVE_B_FOLDER_IDS.strip()
        if not raw:
            return []
        return [fid.strip() for fid in raw.split(",") if fid.strip()]

    # ── Notion ──────────────────────────────────────────────────────────────────
    NOTION_TOKEN:          str = os.environ.get("NOTION_TOKEN", "")
    NOTION_DATABASE_ID:    str = os.environ.get("NOTION_DATABASE_ID", "")
    # 硬碟B專用資料庫（自己的逐字稿總覽）
    NOTION_DATABASE_B_ID:  str = os.environ.get("NOTION_DATABASE_B_ID", "")

    # ── GitHub（選填，推送 Markdown 備份）──────────────────────────────────────
    GITHUB_TOKEN:  str = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_REPO:   str = os.environ.get("GITHUB_REPO", "")
    GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main")
    GITHUB_FOLDER: str = os.environ.get("GITHUB_FOLDER", "audio-summaries")

    # ── Whisper 本地模型 ────────────────────────────────────────────────────────
    # 可選：tiny / base / small / medium / large-v3
    # GitHub Actions 建議用 "small"（速度快，中文準確度夠用）
    # 本機有 GPU 可用 "medium" 或 "large-v3"（最準確）
    WHISPER_MODEL:    str = os.environ.get("WHISPER_MODEL", "small")
    WHISPER_LANGUAGE: str = os.environ.get("WHISPER_LANGUAGE", "zh")  # 留空=自動偵測

    # ── 其他 ────────────────────────────────────────────────────────────────────
    PROCESSED_CACHE:  str = os.environ.get("PROCESSED_CACHE", "processed_files.json")
    SUMMARY_LANGUAGE: str = os.environ.get("SUMMARY_LANGUAGE", "繁體中文")


# ════════════════════════════════════════════════════════════════════════════════
# 已處理追蹤器
# ════════════════════════════════════════════════════════════════════════════════

class ProcessedFilesTracker:
    def __init__(self, cache_file: str, gh_path: str = ""):
        """
        cache_file: 本地 JSON 路徑（GitHub Actions cache 用）
        gh_path:    GitHub repo 內路徑，設定後會從 GitHub 讀取並備份（避免 cache 過期）
        """
        self.cache_file = cache_file
        self.gh_path = gh_path
        self.data: dict = self._load()

    # ── GitHub 備份讀寫 ──────────────────────────────────────────────────────
    @staticmethod
    def _gh_headers() -> dict:
        token = os.environ.get("GITHUB_TOKEN", "")
        return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    def _gh_url(self) -> str:
        repo = os.environ.get("GITHUB_REPO", "")
        branch = os.environ.get("GITHUB_BRANCH", "main")
        return f"https://api.github.com/repos/{repo}/contents/{self.gh_path}?ref={branch}"

    def _load_from_github(self) -> dict:
        """從 GitHub 讀取 tracker JSON（若 GitHub 裡有更新的版本）"""
        try:
            import urllib.request as _urllib
            import base64
            req = _urllib.Request(self._gh_url(), headers=self._gh_headers())
            with _urllib.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                content = base64.b64decode(body["content"]).decode("utf-8")
                return json.loads(content)
        except Exception:
            return {}

    def _save_to_github(self):
        """同步把 tracker JSON 推到 GitHub（確保 GitHub Actions 結束前完成寫入）"""
        import base64, urllib.request as _urllib
        try:
            repo = os.environ.get("GITHUB_REPO", "")
            branch = os.environ.get("GITHUB_BRANCH", "main")
            api_url = f"https://api.github.com/repos/{repo}/contents/{self.gh_path}"
            # 取現有 SHA（PUT 需要 sha 才能更新）
            req = _urllib.Request(api_url + f"?ref={branch}", headers=self._gh_headers())
            try:
                with _urllib.urlopen(req, timeout=10) as r:
                    sha = json.loads(r.read()).get("sha", "")
            except Exception:
                sha = ""
            encoded = base64.b64encode(
                json.dumps(self.data, ensure_ascii=False, indent=2).encode()
            ).decode()
            body = json.dumps({
                "message": f"chore: update {self.gh_path}",
                "content": encoded,
                "branch": branch,
                **({"sha": sha} if sha else {}),
            }).encode()
            put_req = _urllib.Request(api_url, data=body, headers={
                **self._gh_headers(), "Content-Type": "application/json"
            }, method="PUT")
            with _urllib.urlopen(put_req, timeout=15):
                pass
        except Exception as e:
            print(f"[Tracker] GitHub 備份失敗（繼續執行）：{e}")

    # ── 本地讀寫 ────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        local_data: dict = {}
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
            except Exception:
                pass
        # 若有設定 gh_path，再從 GitHub 讀一份做合併（取最多記錄的那份）
        if self.gh_path and os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
            gh_data = self._load_from_github()
            if len(gh_data) > len(local_data):
                print(f"[Tracker] GitHub 版本較新（{len(gh_data)} 筆 > 本地 {len(local_data)} 筆），使用 GitHub 版本")
                local_data = {**local_data, **gh_data}  # 合併，以 GitHub 版為主
        return local_data

    def _save(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        # 同步推到 GitHub（背景）
        if self.gh_path and os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
            self._save_to_github()

    def is_processed(self, file_id: str) -> bool:
        return file_id in self.data

    def is_filename_processed(self, file_name: str) -> bool:
        """檢查是否已有相同檔名的記錄（比對時忽略副檔名，避免 .mp3/.m4a 差異造成漏判）
        注意：若所有匹配記錄都標記為 notion_page_inaccessible，視為未處理，讓 bot 重新建立新頁面。
        """
        stem = Path(file_name).stem
        matched_records = []
        for record in self.data.values():
            rec_name = record.get("file_name", "")
            if rec_name == file_name or Path(rec_name).stem == stem:
                matched_records.append(record)
        if not matched_records:
            return False
        # notion_sync_ 記錄只是「Notion 同步過來的佔位符」，不算真正有效的處理記錄
        # 若去掉 notion_sync_ 佔位符後，剩下的記錄全部都是不可存取的舊頁面，視為未處理
        real_records = [r for r in matched_records if not r.get("processed_at") == "synced_from_notion"]
        if real_records and all(r.get("notion_page_inaccessible") for r in real_records):
            return False
        return True

    def mark_processed(self, file_id: str, metadata: dict):
        self.data[file_id] = {**metadata, "processed_at": datetime.now().isoformat()}
        self._save()

    def all_records(self) -> list:
        return list(self.data.values())


# ════════════════════════════════════════════════════════════════════════════════
# Google Drive 客戶端
# ════════════════════════════════════════════════════════════════════════════════

class GoogleDriveClient:
    def __init__(self, service_account_file: str):
        from google.oauth2 import service_account as sa
        from googleapiclient.discovery import build
        creds = sa.Credentials.from_service_account_file(
            service_account_file,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        self.service = build("drive", "v3", credentials=creds)

    def list_all_audio_files(self, folder_id: str = "root") -> list:
        """列出所有 .mp3 / .m4a 音檔"""
        query = (
            "(mimeType='audio/mpeg' or mimeType='audio/mp4' or "
            "mimeType='audio/x-m4a' or name contains '.mp3' or name contains '.m4a') "
            "and trashed=false"
        )
        if folder_id and folder_id.lower() != "root":
            query += f" and '{folder_id}' in parents"

        files, page_token = [], None
        while True:
            resp = self.service.files().list(
                q=query, spaces="drive", pageToken=page_token, pageSize=100,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, parents)",
            ).execute()
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return files

    def download_file(self, file_id: str, dest_path: str) -> str:
        import time
        from googleapiclient.http import MediaIoBaseDownload
        for attempt in range(1, 4):          # 最多重試 3 次
            try:
                request = self.service.files().get_media(fileId=file_id)
                with open(dest_path, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = dl.next_chunk()
                return dest_path
            except Exception as e:
                if attempt < 3:
                    wait = attempt * 10      # 10秒、20秒後重試
                    print(f"      ⚠️  下載失敗（第{attempt}次），{wait}秒後重試：{e}")
                    time.sleep(wait)
                else:
                    raise                    # 3次都失敗才真正拋錯

    def get_folder_name(self, file_info: dict) -> str:
        try:
            parents = file_info.get("parents", [])
            if not parents:
                return "雲端硬碟根目錄"
            parent = self.service.files().get(fileId=parents[0], fields="name").execute()
            return parent.get("name", "未知資料夾")
        except Exception:
            return "未知資料夾"


# ════════════════════════════════════════════════════════════════════════════════
# faster-whisper 本地語音轉文字（完全免費）
# ════════════════════════════════════════════════════════════════════════════════

class LocalWhisperTranscriber:
    """
    使用 faster-whisper 在本機執行語音轉文字
    - 完全免費，不需要 API 金鑰
    - 模型第一次執行時自動下載（約 150MB～1.5GB）
    - GitHub Actions free runner（2核CPU）：1小時錄音 ≈ 15-25分鐘處理時間
    """

    def __init__(self, model_size: str = "small", language: str = "zh"):
        self.model_size = model_size
        self.language   = language or None
        self._model     = None  # 延遲載入

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            # GitHub Actions 用 CPU；本機有 GPU 會自動使用
            device    = "cuda" if self._has_cuda() else "cpu"
            compute   = "float16" if device == "cuda" else "int8"
            print(f"      📥 載入 Whisper {self.model_size} 模型（{device}）...")
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute)
        return self._model

    @staticmethod
    def _has_cuda() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def transcribe(self, audio_path: str) -> dict:
        """
        回傳：
            text      - 完整逐字稿
            segments  - [{start, end, text}, ...]
            language  - 偵測到的語言
            duration  - 總秒數
        """
        model = self._get_model()
        kwargs = dict(beam_size=5, vad_filter=True)
        if self.language:
            kwargs["language"] = self.language

        segments_iter, info = model.transcribe(audio_path, **kwargs)

        segments, full_text = [], []
        duration = 0.0
        for seg in segments_iter:
            segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
            full_text.append(seg.text.strip())
            duration = seg.end

        return {
            "text":     " ".join(full_text),
            "segments": segments,
            "language": info.language,
            "duration": duration,
        }


# ════════════════════════════════════════════════════════════════════════════════
# Groq 免費 API 摘要生成（Llama 3.3 70B）
# ════════════════════════════════════════════════════════════════════════════════

SUMMARY_PROMPT = """\
你是一位專業的會議/講座記錄整理師，擅長將語音轉錄稿整理成清晰易讀的摘要。
請用{lang}整理以下錄音逐字稿，依以下格式輸出：

## 📋 重點摘要
（3～8 個條列要點，每點 1～2 句，直接點出核心內容）

## 🕐 時間軸
（若錄音超過 5 分鐘，標出各段落的時間點與主題；短於 5 分鐘可略過）

## 💡 行動事項 / 決策
（列出任何待辦事項、決策或後續追蹤；若無則寫「無」）

## 🏷️ 關鍵字標籤
（列出 5～10 個關鍵詞，用逗號分隔）

## 📊 圖表說明（選填）
（如果內容有流程、架構或數據，用 Mermaid 語法畫出；若無則略過）

---
錄音時長：{duration}
偵測語言：{detected_lang}
---

逐字稿：
{transcript}
"""


class GroqSummaryGenerator:
    """
    使用 Groq 免費 API 生成摘要，支援多個 API Key 輪流使用。
    GROQ_API_KEY 可填多個 key，用逗號分隔：key1,key2,key3
    某個 key 每日額度用完時自動切換到下一個。
    """

    def __init__(self, api_key: str, model: str, summary_language: str = "繁體中文"):
        from groq import Groq
        # 支援多個 key（逗號分隔）
        self.api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
        self.key_index = 0
        self.client = Groq(api_key=self.api_keys[0])
        self.model  = model
        self.lang   = summary_language
        if len(self.api_keys) > 1:
            print(f"   🔑 載入 {len(self.api_keys)} 個 Groq API Key，額度用完時自動切換")

    def _next_key(self) -> bool:
        """切換到下一個 API Key，回傳是否還有可用的 key"""
        from groq import Groq
        self.key_index += 1
        if self.key_index >= len(self.api_keys):
            return False
        self.client = Groq(api_key=self.api_keys[self.key_index])
        print(f"      🔄 切換到第 {self.key_index + 1} 個 API Key")
        return True

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds <= 0:
            return "未知"
        h, rem = divmod(int(seconds), 3600)
        m, s   = divmod(rem, 60)
        return f"{h}小時{m}分{s}秒" if h else f"{m}分{s}秒"

    def generate(self, transcript: str, duration: float, detected_lang: str) -> str:
        # 從 4000 字元開始嘗試，若 413（逐字稿太長）則自動縮短後重試
        for max_chars in [4000, 2500, 1200]:
            prompt = SUMMARY_PROMPT.format(
                lang=self.lang,
                duration=self._fmt_duration(duration),
                detected_lang=detected_lang,
                transcript=transcript[:max_chars],
            )
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=2000,
                    messages=[
                        {"role": "system", "content": "你是專業的錄音摘要整理師，用清晰結構呈現重點。"},
                        {"role": "user",   "content": prompt},
                    ],
                )
                return resp.choices[0].message.content
            except Exception as e:
                err_str = str(e)
                # 每日額度用完 → 嘗試切換到下一個 key
                if "rate_limit_exceeded" in err_str and "tokens per day" in err_str:
                    print(f"      ⏸️  第 {self.key_index + 1} 個 Key 每日額度已用完")
                    if self._next_key():
                        continue  # 用新 key 重試
                    raise SystemExit("GROQ_DAILY_LIMIT")  # 所有 key 都用完
                # 單次請求太大（413）→ 縮短逐字稿後重試
                if "413" in err_str or "Request too large" in err_str or "tokens per minute" in err_str.lower():
                    print(f"      ⚠️  逐字稿太長（{max_chars} 字），縮短後重試...")
                    continue
                raise
        raise RuntimeError("逐字稿超過長度上限，即使縮短後仍無法送出")


# ════════════════════════════════════════════════════════════════════════════════
# 輔助函式
# ════════════════════════════════════════════════════════════════════════════════

def extract_keywords(summary_text: str) -> list:
    lines = summary_text.split("\n")
    for i, line in enumerate(lines):
        if "關鍵字" in line or "標籤" in line:
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip().lstrip("- ").strip()
                if candidate:
                    raw = re.split(r"[,，、\s]+", candidate)
                    return [k.strip("#*` ") for k in raw if 1 < len(k.strip()) < 30][:10]
    return []


def extract_mermaid_charts(text: str) -> tuple:
    pattern = r"```mermaid\n(.*?)```"
    charts  = re.findall(pattern, text, re.DOTALL)
    clean   = re.sub(pattern, "", text, flags=re.DOTALL)
    return clean, charts


def build_markdown(
    title: str, file_name: str, folder_name: str,
    duration: float, processed_date: str,
    notion_url: str, summary: str, transcript: str,
) -> str:
    mins, secs = divmod(int(duration), 60)
    return f"""# 🎙️ {title}

> **檔案名稱**：{file_name}
> **資料夾**：{folder_name}
> **時長**：{mins}:{secs:02d}
> **處理時間**：{processed_date}
> **Notion 頁面**：{notion_url}

---

{summary}

---

<details>
<summary>📝 完整逐字稿（點擊展開）</summary>

{transcript}

</details>
"""


# ════════════════════════════════════════════════════════════════════════════════
# Notion 客戶端
# ════════════════════════════════════════════════════════════════════════════════

class NotionClient:
    BASE_URL = "https://api.notion.com/v1"

    def __init__(self, token: str, database_id: str):
        import requests as req_lib
        self._req = req_lib
        self.headers = {
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
            "Notion-Version": "2022-06-28",
        }
        self.database_id = database_id

    def _rt(self, text: str, bold: bool = False) -> list:
        obj = {"type": "text", "text": {"content": text[:2000]}}
        if bold:
            obj["annotations"] = {"bold": True}
        return [obj]

    def _heading(self, level: int, text: str) -> dict:
        key = f"heading_{level}"
        return {"object": "block", "type": key, key: {"rich_text": self._rt(text)}}

    def _paragraph(self, text: str) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": self._rt(text)}}

    def _bullet(self, text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": self._rt(text)}}

    def _numbered(self, text: str) -> dict:
        return {"object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": self._rt(text)}}

    def _md_to_blocks(self, text: str) -> list:
        blocks = []
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("## "):
                blocks.append(self._heading(2, line[3:]))
            elif line.startswith("### "):
                blocks.append(self._heading(3, line[4:]))
            elif re.match(r"^\*\*(.+)\*\*$", line):
                blocks.append(self._heading(3, re.sub(r"^\*\*|\*\*$", "", line)))
            elif line.startswith("- ") or line.startswith("• "):
                blocks.append(self._bullet(line[2:]))
            elif re.match(r"^\d+\.\s", line):
                blocks.append(self._numbered(re.sub(r"^\d+\.\s", "", line)))
            elif line.startswith("---"):
                blocks.append({"object": "block", "type": "divider", "divider": {}})
            else:
                blocks.append(self._paragraph(line))
        return blocks

    def create_child_subpage(self, parent_page_id: str, title: str, transcript: str) -> str:
        """
        在指定 Notion 頁面底下，建立一個逐字稿子頁面。
        parent_page_id 需要含 dash 的格式（直接從 API 回傳的 id 欄位取得）。
        """
        blocks = []
        chunk_size = 1800
        for i in range(0, min(len(transcript), 54000), chunk_size):
            chunk = transcript[i:i + chunk_size]
            blocks.append(self._paragraph(chunk))

        payload = {
            "parent":     {"type": "page_id", "page_id": parent_page_id},
            "icon":       {"emoji": "📝"},
            "properties": {
                "title": [{"text": {"content": title}}]
            },
            "children": blocks[:100],
        }
        resp = self._req.post(f"{self.BASE_URL}/pages", headers=self.headers, json=payload)
        resp.raise_for_status()
        sub_id = resp.json().get("id", "").replace("-", "")
        return f"https://www.notion.so/{sub_id}"

    def create_summary_page(
        self, title: str, file_name: str, folder_name: str,
        duration: float, summary: str, transcript: str,
        keywords: list, processed_date: str, source: str = "",
    ) -> tuple:
        clean_summary, charts = extract_mermaid_charts(summary)
        mins, secs = divmod(int(duration), 60)

        children = [
            {
                "object": "block", "type": "callout",
                "callout": {
                    "rich_text": self._rt(
                        f"📁 資料夾：{folder_name}　｜　⏱ 時長：{mins}:{secs:02d}　｜　📅 {processed_date}"
                    ),
                    "icon": {"emoji": "🎙️"}, "color": "gray_background",
                },
            },
            {"object": "block", "type": "divider", "divider": {}},
        ]

        children.extend(self._md_to_blocks(clean_summary)[:80])

        for chart in charts[:3]:
            children.append({
                "object": "block", "type": "code",
                "code": {"language": "mermaid", "rich_text": self._rt(chart.strip())},
            })

        children.append({"object": "block", "type": "divider", "divider": {}})
        transcript_blocks = [
            self._paragraph(ln) for ln in transcript[:6000].split("\n") if ln.strip()
        ][:30]
        children.append({
            "object": "block", "type": "toggle",
            "toggle": {
                "rich_text": self._rt("📝 完整逐字稿（點擊展開）", bold=True),
                "children":  transcript_blocks,
            },
        })

        # 先用最基本的 properties 嘗試建立頁面，避免欄位不存在造成 400 錯誤
        def _try_create(props: dict) -> dict:
            payload = {
                "parent":     {"database_id": self.database_id},
                "icon":       {"emoji": "🎙️"},
                "properties": props,
                "children":   children[:100],
            }
            resp = self._req.post(f"{self.BASE_URL}/pages", headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()

        # 嘗試完整屬性，失敗就退回只有標題
        full_props = {
            "Name":        {"title":      [{"text": {"content": title[:500]}}]},
            "檔案名稱":    {"rich_text":  [{"text": {"content": file_name[:500]}}]},
            "資料夾":      {"rich_text":  [{"text": {"content": folder_name[:500]}}]},
            "處理日期":    {"date":       {"start": datetime.now().strftime("%Y-%m-%d")}},
            "時長（分鐘）": {"number":    round(duration / 60, 1) if duration > 0 else 0},
            "關鍵字":      {"multi_select": [{"name": k[:100]} for k in keywords[:10]]},
        }
        if source:
            full_props["來源"] = {"select": {"name": source}}
        try:
            data = _try_create(full_props)
        except Exception:
            # 退回只有標題（一定能成功）
            print(f"      ⚠️  完整屬性失敗，改用純標題模式...")
            data = _try_create({"Name": {"title": [{"text": {"content": title[:500]}}]}})

        raw_page_id = data.get("id", "")          # 含 dash，供建立子頁面用
        page_id_clean = raw_page_id.replace("-", "")
        notion_url = f"https://www.notion.so/{page_id_clean}"
        return notion_url, raw_page_id

    def update_folder(self, page_id: str, folder_name: str):
        """更新 Notion 頁面的「資料夾」欄位（頁面移動後同步用）"""
        url = f"{self.BASE_URL}/pages/{page_id}"
        payload = {
            "properties": {
                "資料夾": {"rich_text": [{"type": "text", "text": {"content": folder_name[:500]}}]},
            }
        }
        resp = self._req.patch(url, headers=self.headers, json=payload, timeout=15)
        resp.raise_for_status()


# ════════════════════════════════════════════════════════════════════════════════
# 資料夾移動偵測 & Notion 同步
# ════════════════════════════════════════════════════════════════════════════════

def _notion_url_to_page_id(notion_url: str) -> str:
    """從 Notion URL 取出含 dash 格式的 page ID"""
    path = notion_url.split("?")[0].rstrip("/")
    last = path.split("/")[-1]
    # 移除標題前綴，取最後 32 個 hex 字元
    hex_only = re.sub(r"[^0-9a-fA-F]", "", last)
    raw = hex_only[-32:] if len(hex_only) >= 32 else ""
    if len(raw) == 32:
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return ""


def sync_folder_updates(
    all_files:   list,
    tracker:     "ProcessedFilesTracker",
    drive:       "GoogleDriveClient",
    notion:      "NotionClient",
    drive_label: str = "硬碟A",
) -> int:
    """
    比對 Google Drive 現有資料夾 vs tracker 記錄，
    若檔案已被移到其他資料夾，則更新 Notion 的「資料夾」欄位。
    回傳更新筆數。
    """
    # 建立 file_id → current_folder 對照表
    id_to_folder:   dict = {}
    stem_to_folder: dict = {}   # 給 notion_ 前綴的 key 用（只有 stem，沒有 drive ID）
    for f in all_files:
        folder = drive.get_folder_name(f)
        id_to_folder[f["id"]] = folder
        stem_to_folder[Path(f["name"]).stem] = folder

    # 建立「已知不可存取」的檔名集合，供 notion_sync_ key 跳過用
    inaccessible_stems: set = set()
    for _rec in tracker.data.values():
        if _rec.get("notion_page_inaccessible"):
            _fn = _rec.get("file_name", "")
            if _fn:
                inaccessible_stems.add(Path(_fn).stem)

    updated = 0
    for key, record in tracker.data.items():
        # 已標記為不可存取的頁面，直接跳過不再重試
        if record.get("notion_page_inaccessible"):
            continue
        # notion_sync_ key：若同名檔案已知不可存取，也跳過
        if key.startswith("notion_sync_"):
            _fn = record.get("file_name", "")
            if Path(_fn).stem in inaccessible_stems:
                continue

        stored_folder = record.get("folder_name", "")
        notion_url    = record.get("notion_url", "")
        file_name     = record.get("file_name", "")

        # 找出目前的資料夾
        if key.startswith("notion_"):
            # 從 rebuild 腳本產生的 key，用 stem 比對
            stem = Path(file_name).stem
            current_folder = stem_to_folder.get(stem, "")
        else:
            current_folder = id_to_folder.get(key, "")

        if not current_folder or current_folder == stored_folder:
            continue  # 找不到（可能已刪除）或資料夾未變

        page_id = _notion_url_to_page_id(notion_url)
        if not page_id:
            continue

        print(f"   📁 [{drive_label}] {file_name}：{stored_folder or '（未知）'} → {current_folder}")
        try:
            notion.update_folder(page_id, current_folder)
            tracker.data[key]["folder_name"] = current_folder
            updated += 1
        except Exception as e:
            err_str = str(e)
            if "404" in err_str:
                # 頁面不可存取（舊 token 建立），標記永久跳過，更新 folder_name 避免下次比對
                print(f"      ℹ️  Notion 頁面無法存取（可能由舊 token 建立），標記跳過")
                tracker.data[key]["folder_name"] = current_folder
                tracker.data[key]["notion_page_inaccessible"] = True
                updated += 1
            else:
                print(f"      ⚠️  更新失敗：{e}")

    if updated:
        tracker._save()
        print(f"   ✅ [{drive_label}] 已更新 {updated} 個資料夾記錄")
    else:
        print(f"   ✅ [{drive_label}] 所有資料夾均未變動")

    return updated


# ════════════════════════════════════════════════════════════════════════════════
# GitHub 同步器
# ════════════════════════════════════════════════════════════════════════════════

class GitHubSyncer:
    def __init__(self, token: str, repo: str, branch: str, folder: str):
        import requests as req_lib
        self._req   = req_lib
        self.repo   = repo
        self.branch = branch
        self.folder = folder
        self.headers = {
            "Authorization": f"token {token}",
            "Accept":        "application/vnd.github.v3+json",
        }

    def push_file(self, filename: str, content: str) -> str:
        import base64
        path    = f"{self.folder}/{filename}"
        url     = f"https://api.github.com/repos/{self.repo}/contents/{path}"
        existing = self._req.get(url, headers=self.headers, params={"ref": self.branch})
        sha     = existing.json().get("sha") if existing.status_code == 200 else None
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload = {
            "message": f"{'🔄 Update' if sha else '📝 Add'} summary: {filename}",
            "content": encoded, "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        resp = self._req.put(url, headers=self.headers, json=payload)
        resp.raise_for_status()
        return resp.json().get("content", {}).get("html_url", "")

    def update_index(self, records: list):
        rows = sorted(records, key=lambda x: x.get("processed_at", ""), reverse=True)
        lines = [
            "# 🎙️ 錄音檔摘要索引", "",
            f"> 最後更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "",
            "| 檔案名稱 | 資料夾 | 時長 | 處理日期 | 連結 |",
            "|---------|-------|------|---------|------|",
        ]
        for r in rows:
            name   = r.get("file_name", "")
            folder = r.get("folder_name", "")
            dur    = f"{int(r.get('duration', 0) // 60)}分"
            date   = r.get("processed_at", "")[:10]
            notion = r.get("notion_url", "")
            gh     = r.get("github_url", "")
            link   = f"[Notion]({notion})" + (f" ｜ [MD]({gh})" if gh else "")
            lines.append(f"| {name} | {folder} | {dur} | {date} | {link} |")
        self.push_file("index.md", "\n".join(lines))


# ════════════════════════════════════════════════════════════════════════════════
# 單一音檔處理流程
# ════════════════════════════════════════════════════════════════════════════════

def process_one_file(
    file_info:   dict,
    drive:       GoogleDriveClient,
    transcriber: LocalWhisperTranscriber,
    summarizer:  GroqSummaryGenerator,
    notion:      NotionClient,
    github:      Optional[GitHubSyncer],
    tracker:     ProcessedFilesTracker,
) -> dict:
    file_id   = file_info["id"]
    file_name = file_info["name"]
    print(f"\n  🎙️  {file_name}")

    folder_name = drive.get_folder_name(file_info)
    suffix      = ".m4a" if ".m4a" in file_name.lower() else ".mp3"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        print(f"      ⬇️  下載中...")
        drive.download_file(file_id, tmp_path)

        print(f"      🎧 語音轉文字中（faster-whisper）...")
        td = transcriber.transcribe(tmp_path)
        print(f"         → {GroqSummaryGenerator._fmt_duration(td['duration'])}，偵測語言：{td['language']}")

        print(f"      🤖 生成摘要中（Groq / Llama 3.3）...")
        summary = summarizer.generate(td["text"], td["duration"], td["language"])

        keywords    = extract_keywords(summary)
        today       = datetime.now().strftime("%Y/%m/%d")
        title       = Path(file_name).stem
        proc_date   = datetime.now().strftime("%Y-%m-%d %H:%M")

        print(f"      📓 建立 Notion 頁面...")
        notion_url, _ = notion.create_summary_page(
            title=title, file_name=file_name, folder_name=folder_name,
            duration=td["duration"], summary=summary, transcript=td["text"],
            keywords=keywords, processed_date=proc_date,
        )

        github_url = ""
        if github:
            print(f"      🐙 同步至 GitHub...")
            md_content = build_markdown(
                title=title, file_name=file_name, folder_name=folder_name,
                duration=td["duration"], processed_date=proc_date,
                notion_url=notion_url, summary=summary, transcript=td["text"],
            )
            safe     = re.sub(r"[^\w\-]", "_", Path(file_name).stem)
            md_fname = f"{datetime.now().strftime('%Y%m%d')}_{safe}.md"
            try:
                github_url = github.push_file(md_fname, md_content)
            except Exception as e:
                print(f"      ⚠️  GitHub 同步失敗：{e}")

        tracker.mark_processed(file_id, {
            "file_name": file_name, "folder_name": folder_name,
            "duration": td["duration"], "notion_url": notion_url,
            "github_url": github_url,
        })

        print(f"      ✅ 完成！→ {notion_url}")
        return {"ok": True, "file_name": file_name, "notion_url": notion_url}

    except Exception as exc:
        err_str = str(exc)
        # Groq 每日 token 上限 → 停止當天所有後續處理，明天繼續
        if "rate_limit_exceeded" in err_str and "tokens per day" in err_str:
            print(f"      ⏸️  Groq 每日免費額度已用完，今天先到這裡，明天繼續！")
            raise SystemExit("GROQ_DAILY_LIMIT")
        print(f"      ❌ 失敗：{exc}")
        return {"ok": False, "file_name": file_name, "error": err_str}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ════════════════════════════════════════════════════════════════════════════════
# 雲端硬碟 B：處理單一音檔（建立 Notion 摘要頁面 + 逐字稿子頁面）
# ════════════════════════════════════════════════════════════════════════════════

def process_drive_b_file(
    file_info:   dict,
    drive:       GoogleDriveClient,
    transcriber: LocalWhisperTranscriber,
    summarizer:  GroqSummaryGenerator,
    notion_b:    NotionClient,          # 指向「自己的逐字稿總覽」資料庫
    tracker_b:   ProcessedFilesTracker,
    tracker_a:   ProcessedFilesTracker,
) -> dict:
    """
    處理雲端硬碟 B 的音檔：
    1. 若檔名與硬碟 A 相同 → 跳過
    2. 轉錄 → 生成摘要
    3. 寫入「自己的逐字稿總覽」資料庫（NOTION_DATABASE_B_ID）
    """
    file_id   = file_info["id"]
    file_name = file_info["name"]

    # 已處理（硬碟 B 自身快取）
    if tracker_b.is_processed(file_id) or tracker_b.is_filename_processed(file_name):
        return {"ok": True, "file_name": file_name, "skipped": True, "reason": "B已處理"}

    # 與硬碟 A 同名 → 跳過
    if tracker_a.is_filename_processed(file_name):
        print(f"\n  ⏭️  {file_name}（與硬碟A同名，跳過）")
        tracker_b.mark_processed(file_id, {
            "file_name": file_name, "skipped": True, "reason": "同名硬碟A",
        })
        return {"ok": True, "file_name": file_name, "skipped": True, "reason": "同名硬碟A"}

    print(f"\n  🗂️  [硬碟B] {file_name}")
    folder_name = drive.get_folder_name(file_info)
    suffix      = ".m4a" if ".m4a" in file_name.lower() else ".mp3"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        print(f"      ⬇️  下載中...")
        drive.download_file(file_id, tmp_path)

        print(f"      🎧 語音轉文字中（faster-whisper）...")
        td = transcriber.transcribe(tmp_path)
        print(f"         → {GroqSummaryGenerator._fmt_duration(td['duration'])}，偵測語言：{td['language']}")

        print(f"      🤖 生成摘要中（Groq / Llama 3.3）...")
        summary = summarizer.generate(td["text"], td["duration"], td["language"])

        keywords  = extract_keywords(summary)
        title     = Path(file_name).stem
        proc_date = datetime.now().strftime("%Y-%m-%d %H:%M")

        print(f"      📓 寫入「自己的逐字稿總覽」資料庫...")
        notion_url, _ = notion_b.create_summary_page(
            title=title, file_name=file_name, folder_name=folder_name,
            duration=td["duration"], summary=summary, transcript=td["text"],
            keywords=keywords, processed_date=proc_date,
        )

        tracker_b.mark_processed(file_id, {
            "file_name": file_name, "folder_name": folder_name,
            "duration": td["duration"], "notion_url": notion_url,
        })

        print(f"      ✅ 完成！→ {notion_url}")
        return {"ok": True, "file_name": file_name, "notion_url": notion_url}

    except Exception as exc:
        err_str = str(exc)
        if "rate_limit_exceeded" in err_str and "tokens per day" in err_str:
            print(f"      ⏸️  Groq 每日免費額度已用完，今天先到這裡，明天繼續！")
            raise SystemExit("GROQ_DAILY_LIMIT")
        print(f"      ❌ 失敗：{exc}")
        return {"ok": False, "file_name": file_name, "error": err_str}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ════════════════════════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════════════════════════

def validate_config(cfg: Config) -> list:
    missing = []
    if not cfg.GROQ_API_KEY:
        missing.append("GROQ_API_KEY  ← 申請網址：https://console.groq.com/keys（免費）")
    if not cfg.NOTION_TOKEN:
        missing.append("NOTION_TOKEN  ← 申請網址：https://www.notion.so/my-integrations（免費）")
    if not cfg.NOTION_DATABASE_ID:
        missing.append("NOTION_DATABASE_ID  ← 從 Notion 資料庫 URL 複製")
    if not os.path.exists(cfg.GOOGLE_SERVICE_ACCOUNT_FILE):
        missing.append(f"Google 服務帳號金鑰：{cfg.GOOGLE_SERVICE_ACCOUNT_FILE}（請見 README.md）")
    return missing


def main():
    cfg = Config()
    print("🎙️  錄音檔摘要機器人（免費版）\n" + "=" * 50)
    print(f"   Whisper 模型：{cfg.WHISPER_MODEL}（本地執行，完全免費）")
    print(f"   AI 摘要：Groq {cfg.GROQ_MODEL}（免費 API）\n")

    missing = validate_config(cfg)
    if missing:
        print("❌ 以下設定未完成：")
        for m in missing:
            print(f"   • {m}")
        print("\n📖 詳細說明請看 README.md")
        sys.exit(1)

    # 初始化模組
    print("🔌 連接 Google Drive...")
    drive       = GoogleDriveClient(cfg.GOOGLE_SERVICE_ACCOUNT_FILE)
    transcriber = LocalWhisperTranscriber(cfg.WHISPER_MODEL, cfg.WHISPER_LANGUAGE)
    summarizer  = GroqSummaryGenerator(cfg.GROQ_API_KEY, cfg.GROQ_MODEL, cfg.SUMMARY_LANGUAGE)
    notion      = NotionClient(cfg.NOTION_TOKEN, cfg.NOTION_DATABASE_ID)
    tracker     = ProcessedFilesTracker(cfg.PROCESSED_CACHE, gh_path="processed_files.json")

    # ── 從 Notion 同步已處理的檔案名稱（防止快取遺失時重複處理）────────────
    print("🔄 從 Notion 同步已處理記錄...")
    try:
        import requests as _req
        _headers = {
            "Authorization": f"Bearer {cfg.NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        }
        _has_more, _cursor = True, None
        _notion_filenames = set()
        while _has_more:
            _body = {"page_size": 100}
            if _cursor:
                _body["start_cursor"] = _cursor
            _resp = _req.post(
                f"https://api.notion.com/v1/databases/{cfg.NOTION_DATABASE_ID}/query",
                headers=_headers, json=_body, timeout=15
            )
            if _resp.status_code == 200:
                _data = _resp.json()
                for _page in _data.get("results", []):
                    # 跳過已刪除（在垃圾桶）或已封存的頁面
                    if _page.get("archived") or _page.get("in_trash"):
                        continue
                    _rt = _page.get("properties", {}).get("檔案名稱", {}).get("rich_text", [])
                    _fname = "".join(r.get("plain_text", "") for r in _rt)
                    if _fname:
                        _notion_filenames.add(_fname)
                _has_more = _data.get("has_more", False)
                _cursor   = _data.get("next_cursor")
            else:
                break
        # 把 Notion 已有的檔案名稱加入 tracker（用假 ID 標記為已處理）
        _synced = 0
        for _fname in _notion_filenames:
            _fake_id = f"notion_sync_{_fname}"
            if not tracker.is_processed(_fake_id) and not tracker.is_filename_processed(_fname):
                tracker.data[_fake_id] = {"file_name": _fname, "processed_at": "synced_from_notion"}
                _synced += 1
        if _synced:
            tracker._save()
        print(f"   ✓ Notion 已有 {len(_notion_filenames)} 個檔案，同步 {_synced} 筆新記錄")
    except Exception as _e:
        print(f"   ⚠️  Notion 同步失敗（不影響執行）：{_e}")

    github: Optional[GitHubSyncer] = None
    if cfg.GITHUB_TOKEN and cfg.GITHUB_REPO:
        github = GitHubSyncer(cfg.GITHUB_TOKEN, cfg.GITHUB_REPO, cfg.GITHUB_BRANCH, cfg.GITHUB_FOLDER)
        print(f"🐙 GitHub 同步：{cfg.GITHUB_REPO}")

    # 掃描 Google Drive（支援多個資料夾）
    folder_ids = cfg.folder_id_list
    print(f"\n📂 掃描 Google Drive（共 {len(folder_ids)} 個資料夾）...")

    seen_ids  = set()
    all_files = []
    for fid in folder_ids:
        # 取得資料夾實際名稱
        if fid == "root":
            label = "整個雲端硬碟"
        else:
            try:
                folder_info = drive.service.files().get(fileId=fid, fields="name").execute()
                label = f"{folder_info.get('name', fid)}（{fid}）"
            except Exception:
                label = fid
        print(f"   🔍 掃描：{label}")
        found = drive.list_all_audio_files(fid)
        for f in found:
            if f["id"] not in seen_ids:   # 避免同一檔案出現在多個資料夾時重複
                seen_ids.add(f["id"])
                all_files.append(f)
        print(f"      → 找到 {len(found)} 個音檔")

    new_files = [f for f in all_files
                 if not tracker.is_processed(f["id"])
                 and not tracker.is_filename_processed(f["name"])]
    print(f"\n   合計 {len(all_files)} 個音檔，{len(new_files)} 個尚未處理")

    # ── 資料夾移動偵測（硬碟A）──────────────────────────────────────────────
    print("\n🔄 偵測硬碟A 資料夾變動...")
    sync_folder_updates(all_files, tracker, drive, notion, drive_label="硬碟A")

    # 逐一處理硬碟A
    results = []
    if not new_files:
        print("\n✅ 硬碟A 沒有新的錄音檔。")
    else:
        print(f"\n{'─' * 50}")
        for fi in new_files:
            try:
                result = process_one_file(fi, drive, transcriber, summarizer, notion, github, tracker)
                results.append(result)
            except SystemExit as e:
                if str(e) == "GROQ_DAILY_LIMIT":
                    print(f"\n⏸️  今日 Groq 免費額度用完，已處理 {len(results)} 個。")
                    print(f"   剩餘 {len(new_files) - len(results)} 個，明天自動繼續。")
                    break
                raise

        # 更新 GitHub 索引
        if github:
            try:
                github.update_index(tracker.all_records())
                print("\n🐙 GitHub 索引已更新")
            except Exception as e:
                print(f"\n⚠️  GitHub 索引更新失敗：{e}")

        ok  = sum(1 for r in results if r.get("ok"))
        err = sum(1 for r in results if not r.get("ok"))
        print(f"\n{'=' * 50}")
        print(f"✅ 硬碟A 完成！成功 {ok} 個，失敗 {err} 個")
        if err:
            for r in results:
                if not r["ok"]:
                    print(f"   ❌ {r['file_name']}: {r.get('error','')}")

    # ──────────────────────────────────────────────────────────────────────────
    # 處理雲端硬碟 B（用剩餘 Groq 額度，優先處理硬碟 A 後才執行）
    # ──────────────────────────────────────────────────────────────────────────
    drive_b_folders = cfg.drive_b_folder_id_list
    if not drive_b_folders:
        print("\n📂 未設定 DRIVE_B_FOLDER_IDS，跳過硬碟 B。")
        return

    if not cfg.NOTION_DATABASE_B_ID:
        print("\n⚠️  未設定 NOTION_DATABASE_B_ID，跳過硬碟 B。")
        return

    # 建立指向「自己的逐字稿總覽」的 Notion 客戶端
    notion_b = NotionClient(cfg.NOTION_TOKEN, cfg.NOTION_DATABASE_B_ID)

    print(f"\n{'─' * 50}")
    print(f"📂 [硬碟B] 掃描（共 {len(drive_b_folders)} 個資料夾）...")

    tracker_b = ProcessedFilesTracker(
        cfg.DRIVE_B_PROCESSED_CACHE,
        gh_path="processed_files_b.json",  # GitHub 備份，避免 Actions cache 過期後重複上傳
    )

    seen_b_ids = set()
    all_b_files = []
    for fid in drive_b_folders:
        try:
            folder_info = drive.service.files().get(fileId=fid, fields="name").execute()
            label = f"{folder_info.get('name', fid)}（{fid}）"
        except Exception:
            label = fid
        print(f"   🔍 [硬碟B] 掃描：{label}")
        found = drive.list_all_audio_files(fid)
        for f in found:
            if f["id"] not in seen_b_ids:
                seen_b_ids.add(f["id"])
                all_b_files.append(f)
        print(f"      → 找到 {len(found)} 個音檔")

    # 過濾：已處理的 + 與硬碟A同名的直接跳過（不計入待處理）
    # 同時對「尚未處理的檔案」按檔名去重，避免同名不同 ID 的檔案在同一次 run 重複上傳
    _seen_new_b_names: set = set()
    new_b_files = []
    for f in all_b_files:
        if (not tracker_b.is_processed(f["id"])
                and not tracker_b.is_filename_processed(f["name"])
                and not tracker.is_filename_processed(f["name"])  # tracker = 硬碟A
                and f["name"] not in _seen_new_b_names):
            _seen_new_b_names.add(f["name"])
            new_b_files.append(f)
    same_name_count = sum(
        1 for f in all_b_files
        if tracker.is_filename_processed(f["name"])
        and not tracker_b.is_processed(f["id"])
    )
    print(f"\n   合計 {len(all_b_files)} 個音檔，"
          f"{len(new_b_files)} 個待處理，{same_name_count} 個與硬碟A同名跳過")

    # ── 資料夾移動偵測（硬碟B）──────────────────────────────────────────────
    print("\n🔄 偵測硬碟B 資料夾變動...")
    sync_folder_updates(all_b_files, tracker_b, drive, notion_b, drive_label="硬碟B")

    if not new_b_files:
        print("\n✅ 硬碟B 沒有新的錄音檔，結束。")
        return

    print(f"\n{'─' * 50}")
    results_b = []
    for fi in new_b_files:
        try:
            result = process_drive_b_file(
                fi, drive, transcriber, summarizer, notion_b,
                tracker_b, tracker,
            )
            results_b.append(result)
        except SystemExit as e:
            if str(e) == "GROQ_DAILY_LIMIT":
                print(f"\n⏸️  [硬碟B] 今日 Groq 免費額度用完，已處理 {len(results_b)} 個。")
                print(f"   剩餘 {len(new_b_files) - len(results_b)} 個，明天自動繼續。")
                break
            raise

    ok_b  = sum(1 for r in results_b if r.get("ok") and not r.get("skipped"))
    err_b = sum(1 for r in results_b if not r.get("ok"))
    print(f"\n{'=' * 50}")
    print(f"✅ 硬碟B 完成！成功 {ok_b} 個，失敗 {err_b} 個")


if __name__ == "__main__":
    main()
