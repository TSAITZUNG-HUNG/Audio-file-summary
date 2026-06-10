#!/usr/bin/env python3
"""
🤖 LINE 錄音檔推薦機器人
================================
使用者用自然語言說出需求，AI 從 Notion 錄音檔摘要資料庫推薦最適合的 5 個，
並提供 Notion 摘要連結；選擇後傳送 Google Drive 共享連結。
 
費用：$0（Groq 免費 API + Render 免費方案 + LINE OA 免費）
"""
 
import os
import re
import json
import threading
import time
from datetime import datetime, timedelta
 
import requests as req_lib
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent, MemberJoinedEvent
from groq import Groq
 
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
# ════════════════════════════════════════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════════════════════════════════════════
 
app = Flask(__name__)
 
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
handler     = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
 
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
groq_client   = Groq(api_key=GROQ_API_KEY)
 
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
 
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
 
# 通關密碼（在 Render 環境變數 BOT_PASSWORD 設定，隨時可改）
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "")
# 已驗證的使用者 ID（in-memory，重啟後需重新輸入密碼）
_authorized_users: set = set()
 
# ── In-memory Session ────────────────────────────────────────────────────────
_sessions: dict = {}
SESSION_TTL_MIN = 10
 
def _get_session(user_id: str) -> dict:
    s = _sessions.get(user_id, {})
    if s and datetime.now() - s.get("ts", datetime.min) > timedelta(minutes=SESSION_TTL_MIN):
        _sessions.pop(user_id, None)
        return {}
    return s
 
def _set_session(user_id: str, data: dict):
    _sessions[user_id] = {**data, "ts": datetime.now()}
 
def _clear_session(user_id: str):
    _sessions.pop(user_id, None)
 
 
# ════════════════════════════════════════════════════════════════════════════════
# Google Drive 客戶端
# ════════════════════════════════════════════════════════════════════════════════
 
_drive_service = None
_bot_user_id = None  # 機器人自己的 LINE user ID（啟動時自動取得）
 
def _get_bot_user_id() -> str:
    """取得機器人自己的 LINE user ID，用於精確判斷 @ 提及"""
    global _bot_user_id
    if _bot_user_id is None:
        try:
            with ApiClient(line_config) as api_client:
                info = MessagingApi(api_client).get_bot_info()
                _bot_user_id = info.user_id
                print(f"[Bot] Bot user ID 已載入：{_bot_user_id[:8]}...")
        except Exception as e:
            print(f"[Bot] 取得 Bot ID 失敗（不影響執行）：{e}")
    return _bot_user_id
 
def _get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service
 
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
 
    if GOOGLE_SA_JSON:
        info = json.loads(GOOGLE_SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SA_FILE,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
 
    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service
 
 
def get_drive_share_link(file_name: str) -> str:
    try:
        service = _get_drive_service()
        safe_name = file_name.replace("'", "\\'")
        results = service.files().list(
            q=f"name='{safe_name}' and trashed=false",
            fields="files(id, name, webViewLink)",
            pageSize=1
        ).execute()
 
        files = results.get("files", [])
        if not files:
            print(f"[Drive] 找不到檔案：{file_name}")
            return ""
 
        file_id = files[0]["id"]
 
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
            ).execute()
        except Exception as perm_err:
            print(f"[Drive] 設定權限警告：{perm_err}")
 
        info = service.files().get(fileId=file_id, fields="webViewLink, name").execute()
        link = info.get("webViewLink", "")
        print(f"[Drive] 取得連結：{file_name} → {link}")
        return link
 
    except Exception as e:
        print(f"[Drive] 錯誤：{e}")
        return ""
 
 
# ════════════════════════════════════════════════════════════════════════════════
# Notion 讀取器
# ════════════════════════════════════════════════════════════════════════════════
 
def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
 
def _get_rich_text(props: dict, key: str) -> str:
    rt = props.get(key, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rt)
 
 
def fetch_all_summaries(max_pages: int = 400) -> list:
    headers = _notion_headers()
    summaries = []
    has_more = True
    start_cursor = None
 
    while has_more and len(summaries) < max_pages:
        body: dict = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
 
        resp = req_lib.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=headers, json=body, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
 
        for page in data.get("results", []):
            props = page.get("properties", {})
 
            title_arr = props.get("Name", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_arr)
 
            file_name   = _get_rich_text(props, "檔案名稱")
            folder_name = _get_rich_text(props, "資料夾")
            duration    = props.get("時長（分鐘）", {}).get("number") or 0
            keywords    = [k["name"] for k in props.get("關鍵字", {}).get("multi_select", [])]
            page_id_clean = page["id"].replace("-", "")
            notion_url  = f"https://www.notion.so/{page_id_clean}"
 
            summary_text = ""
 
            if not title and not file_name:
                continue
 
            summaries.append({
                "id":           page["id"],
                "title":        title,
                "file_name":    file_name,
                "folder":       folder_name,
                "duration_min": round(duration, 1),
                "keywords":     keywords,
                "notion_url":   notion_url,
                "summary_text": summary_text,
            })
 
        has_more     = data.get("has_more", False)
        start_cursor = data.get("next_cursor")
 
    print(f"[Notion] 共讀取 {len(summaries)} 筆摘要")
    return summaries
 
 
# ════════════════════════════════════════════════════════════════════════════════
# AI 推薦引擎
# ════════════════════════════════════════════════════════════════════════════════
 
def _pre_filter(question: str, summaries: list, top_n: int = 50) -> list:
    """
    快速關鍵字預篩選（純 Python，不需要 API）。
    計算問題字元與每個摘要（標題＋關鍵字）的重疊度，取前 top_n 個。
    同時去除重複標題（只保留分數最高的那個），避免 AI 推薦同名錄音檔。
    """
    q_chars = set(question.replace(" ", "").replace("，", "").replace("。", ""))
    scored = []
    for s in summaries:
        text = s["title"] + "".join(s["keywords"])
        t_chars = set(text)
        score = len(q_chars & t_chars)
        scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
 
    # 去除重複標題（移除日期後的標題相同就算重複，保留分數最高的）
    seen_titles = set()
    deduped = []
    for score, s in scored:
        base_title = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", s["title"]).strip()
        if base_title not in seen_titles:
            seen_titles.add(base_title)
            deduped.append(s)
        if len(deduped) >= top_n:
            break
 
    return deduped if deduped else summaries[:top_n]
 
 
def recommend_transcripts(question: str, summaries: list) -> list:
    # 先用關鍵字預篩選，從所有摘要中取最相關的 50 個
    candidates = _pre_filter(question, summaries, top_n=50)
    print(f"[AI] 從 {len(summaries)} 個摘要預篩出 {len(candidates)} 個候選")
 
    index_lines = []
    for i, s in enumerate(candidates):
        kw  = "、".join(s["keywords"][:5]) if s["keywords"] else "無"
        dur = f"{s['duration_min']:.0f}分鐘" if s["duration_min"] else "不明"
        index_lines.append(
            f"[{i+1}] 《{s['title']}》 關鍵字：{kw} 時長：{dur}"
        )
 
    context = "\n".join(index_lines)
    if len(context) > 5000:
        context = context[:5000] + "\n...(以下省略)"
 
    prompt = f"""使用者的問題／需求：
「{question}」
 
以下是與問題最相關的錄音檔候選（從 {len(summaries)} 個中預篩出 {len(candidates)} 個）：
 
{context}
 
請根據使用者的問題，選出最適合的 5 個錄音檔，並說明原因。
 
請嚴格按照以下格式輸出（不要有其他文字）：
RECOMMEND:編號1,編號2,編號3,編號4,編號5
REASON1:推薦原因（1-2句，直接說明對使用者的幫助，不要重複標題）
REASON2:推薦原因
REASON3:推薦原因
REASON4:推薦原因
REASON5:推薦原因
"""
 
    # 最多重試 3 次（429 rate limit 時等待後重試）
    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=600,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": "你是錄音檔推薦助手，根據使用者需求推薦最相關的內容。"},
                    {"role": "user",   "content": prompt},
                ],
            )
            break
        except Exception as e:
            err = str(e)
            if "429" in err and attempt < 2:
                print(f"[Groq] 429 rate limit，等待 30 秒後重試（第 {attempt+1} 次）")
                time.sleep(30)
                continue
            raise
 
    content = resp.choices[0].message.content.strip()
    print(f"[Groq] 推薦結果：\n{content}")
 
    rec_match = re.search(r"RECOMMEND\s*[:：]\s*(.+)", content)
    if not rec_match:
        return []
 
    raw_indices = re.findall(r"\d+", rec_match.group(1))
    indices = [int(x) - 1 for x in raw_indices if x.isdigit()]
 
    reasons = {}
    for i in range(1, 6):
        m = re.search(rf"REASON{i}\s*[:：]\s*(.+)", content)
        if m:
            reason = m.group(1).strip()
            # 移除 AI 有時加的「[N] 標題」前綴
            reason = re.sub(r"^\[\d+\]\s*《[^》]*》\s*", "", reason).strip()
            reason = re.sub(r"^\[\d+\]\s*\S+.*?(?:—\s*\d{4}/\d{2}/\d{2})?\s*", "", reason).strip()
            reasons[i] = reason
        else:
            reasons[i] = ""
 
    result = []
    for rank, idx in enumerate(indices[:5], 1):
        if 0 <= idx < len(candidates):
            item = candidates[idx].copy()
            item["reason"] = reasons.get(rank, "")
            result.append(item)
 
    return result
 
 
# ════════════════════════════════════════════════════════════════════════════════
# LINE 訊息格式化
# ════════════════════════════════════════════════════════════════════════════════
 
_CIRCLE = ["①", "②", "③", "④", "⑤"]
 
def format_recommendations(items: list, question: str) -> str:
    lines = [
        f'根據您的需求：「{question[:50]}」\n我推薦以下 5 個錄音檔：\n',
    ]
    for i, item in enumerate(items):
        dur   = f"{item['duration_min']:.0f}分鐘" if item["duration_min"] else ""
        title = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", item["title"]).strip()
        drive_link = item.get("drive_link", "")
        notion_url = item.get("notion_url", "")
        block = (
            f"{_CIRCLE[i]} {title}　⏱ {dur}\n"
            f"📖 摘要：{notion_url}\n"
        )
        if drive_link:
            block += f"📥 音檔：{drive_link}\n"
        lines.append(block)
    return "\n".join(lines)
 
 
# ════════════════════════════════════════════════════════════════════════════════
# 背景任務處理
# ════════════════════════════════════════════════════════════════════════════════
 
def _handle_list_reply(user_id: str, reply_token: str, push_target: str, keyword: str = ""):
    """
    同步執行 /list，用 reply_message 送出（免費，不佔月額度）。
    LINE reply 最多 5 則訊息，超過的用 push 補送。
    """
    try:
        summaries = fetch_all_summaries()
        if not summaries:
            _reply(reply_token, "⚠️ 目前資料庫沒有錄音檔摘要。")
            return
 
        if keyword:
            filtered = [s for s in summaries if keyword in s["title"] or keyword in "".join(s["keywords"]) or keyword in s.get("folder", "")]
            if not filtered:
                _reply(reply_token, f"找不到含有「{keyword}」的錄音檔。")
                return
        else:
            filtered = summaries
 
        from collections import defaultdict
        folder_map = defaultdict(list)
        for s in filtered:
            folder = s.get("folder", "").strip() or "未分類"
            folder_map[folder].append(s)
 
        sorted_folders = sorted(folder_map.keys(), key=lambda x: ("zzz" if x == "未分類" else x))
 
        ordered = []
        for folder in sorted_folders:
            for s in folder_map[folder]:
                ordered.append(s)
 
        _set_session(user_id, {"state": "listing", "items": ordered, "push_target": push_target})
 
        # 建立訊息列表
        if keyword:
            header = f"🔍 搜尋「{keyword}」，找到 {len(filtered)} 個錄音檔："
        else:
            header = f"📋 共 {len(filtered)} 個錄音檔，依資料夾分類："
 
        global_idx = 1
        current_lines = [header, ""]
        messages = []
 
        for folder in sorted_folders:
            items_in_folder = folder_map[folder]
            folder_lines = [f"📁 {folder}（{len(items_in_folder)} 個）"]
            for s in items_in_folder:
                title = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", s["title"]).strip()
                dur = f"{s['duration_min']:.0f}分" if s["duration_min"] else ""
                folder_lines.append(f"{global_idx}. {title}　{dur}")
                global_idx += 1
            if len(current_lines) + len(folder_lines) > 35:
                messages.append("\n".join(current_lines).strip())
                current_lines = folder_lines + [""]
            else:
                current_lines += folder_lines + [""]
 
        if current_lines:
            messages.append("\n".join(current_lines).strip())
        messages.append("💡 輸入編號即可取得該錄音檔的 Google Drive 連結！\n（支援多個編號，如：1,3,5）")
 
        # 前 5 則用免費 reply，超過的用 push
        reply_msgs = [TextMessage(text=m) for m in messages[:5]]
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=reply_msgs)
            )
        for m in messages[5:]:
            _push(push_target, m)
 
    except Exception as e:
        print(f"[List] 錯誤：{e}")
        try:
            _reply(reply_token, f"❌ 載入清單時發生錯誤：{str(e)[:100]}")
        except Exception:
            _push(push_target, f"❌ 載入清單時發生錯誤：{str(e)[:100]}")
 
 
def _bg_handle_list(user_id: str, push_target: str, keyword: str = ""):
    """背景執行：按資料夾分類列出錄音檔，支援關鍵字篩選"""
    try:
        summaries = fetch_all_summaries()
        if not summaries:
            _push(push_target, "⚠️ 目前資料庫沒有錄音檔摘要。")
            return
 
        # 關鍵字篩選
        if keyword:
            filtered = [s for s in summaries if keyword in s["title"] or keyword in "".join(s["keywords"]) or keyword in s.get("folder", "")]
            if not filtered:
                _push(push_target, f"找不到含有「{keyword}」的錄音檔。")
                return
        else:
            filtered = summaries
 
        # 按資料夾分組並排序
        from collections import defaultdict
        folder_map = defaultdict(list)
        for s in filtered:
            folder = s.get("folder", "").strip() or "未分類"
            folder_map[folder].append(s)
 
        # 資料夾名稱排序（未分類放最後）
        sorted_folders = sorted(
            folder_map.keys(),
            key=lambda x: ("zzz" if x == "未分類" else x)
        )
 
        # 建立全域編號列表（儲存到 session 供選擇用）
        ordered = []
        for folder in sorted_folders:
            for s in folder_map[folder]:
                ordered.append(s)
 
        _set_session(user_id, {
            "state": "listing",
            "items": ordered,
            "push_target": push_target,
        })
 
        # 標題
        if keyword:
            header = f"🔍 搜尋「{keyword}」，找到 {len(filtered)} 個錄音檔："
        else:
            header = f"📋 共 {len(filtered)} 個錄音檔，依資料夾分類："
 
        # 把多個資料夾合併，每則訊息最多 35 行，減少訊息數量避免被中斷
        global_idx = 1
        current_lines = [header, ""]
        messages = []
 
        for folder in sorted_folders:
            items_in_folder = folder_map[folder]
            folder_lines = [f"📁 {folder}（{len(items_in_folder)} 個）"]
            for s in items_in_folder:
                title = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", s["title"]).strip()
                dur = f"{s['duration_min']:.0f}分" if s["duration_min"] else ""
                folder_lines.append(f"{global_idx}. {title}　{dur}")
                global_idx += 1
 
            # 若加進去超過 35 行就先送出，開新訊息
            if len(current_lines) + len(folder_lines) > 35:
                messages.append("\n".join(current_lines))
                current_lines = folder_lines + [""]
            else:
                current_lines += folder_lines + [""]
 
        if current_lines:
            messages.append("\n".join(current_lines))
 
        messages.append("💡 輸入編號即可取得該錄音檔的 Google Drive 連結！\n（支援多個編號，如：1,3,5）")
 
        # 連續送出，不加 sleep
        for msg in messages:
            _push(push_target, msg.strip())
 
    except Exception as e:
        print(f"[BG] 列表錯誤：{e}")
        _push(push_target, f"❌ 載入清單時發生錯誤：{str(e)[:100]}")
 
 
def _bg_handle_question(user_id: str, push_target: str, question: str):
    try:
        summaries = fetch_all_summaries()
        if not summaries:
            _push(push_target, "⚠️ Notion 資料庫目前沒有摘要，請先執行錄音檔摘要機器人產生摘要。")
            return
 
        items = recommend_transcripts(question, summaries)
        if not items:
            _push(push_target, "😅 找不到合適的推薦，請換個方式描述您的需求，例如：\n「我想學習如何開發客戶」\n「推薦我關於時間管理的錄音」")
            return
 
        # 同時取得每個推薦的 Drive 連結
        print(f"[BG] 取得 {len(items)} 個 Drive 連結...")
        for item in items:
            fname = item.get("file_name", "")
            if fname:
                item["drive_link"] = get_drive_share_link(fname)
            else:
                item["drive_link"] = ""
 
        # 直接輸出推薦＋連結，不需要使用者再選號
        _push(push_target, format_recommendations(items, question))
 
    except Exception as e:
        print(f"[BG] 問題處理錯誤：{e}")
        _push(push_target, f"❌ 處理時發生錯誤，請稍後再試。\n（{str(e)[:100]}）")
 
 
def _handle_selection_reply(reply_token: str, push_target: str, indices: list, items: list):
    """
    同步取得 Drive 連結並用 reply_message 回覆（免費，不佔月額度）。
    reply 最多 5 則，超過的才用 push。
    """
    try:
        messages = []
        for idx in indices[:5]:  # 最多處理 5 個
            if idx < 0 or idx >= len(items):
                continue
            item  = items[idx]
            fname = item.get("file_name", "")
            link  = get_drive_share_link(fname) if fname else ""
            title = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", item.get("title", fname)).strip()
            notion_url = item.get("notion_url", "")
 
            if link:
                msg = (f"🎙️ {title}\n\n"
                       f"📥 Google Drive 播放連結：\n{link}\n\n"
                       f"📖 Notion 完整摘要：\n{notion_url}")
            else:
                msg = (f"🎙️ {title}\n\n"
                       f"⚠️ 無法取得 Google Drive 連結\n\n"
                       f"📖 Notion 完整摘要：\n{notion_url}")
            messages.append(msg)
 
        if not messages:
            _reply(reply_token, "❌ 找不到對應的錄音檔。")
            return
 
        # 用 reply 送出（免費），最多 5 則
        reply_msgs = [TextMessage(text=m) for m in messages[:5]]
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=reply_msgs)
            )
        # 超過 5 個才用 push（極少發生）
        for m in messages[5:]:
            _push(push_target, m)
 
    except Exception as e:
        print(f"[Selection] 錯誤：{e}")
        try:
            _reply(reply_token, f"❌ 取得連結時發生錯誤：{str(e)[:100]}")
        except Exception:
            _push(push_target, f"❌ 取得連結時發生錯誤：{str(e)[:100]}")
 
 
def _bg_handle_selection(push_target: str, idx: int, item: dict):
    """取得單一檔案的 Drive 連結"""
    try:
        file_name  = item.get("file_name", "")
        link       = get_drive_share_link(file_name) if file_name else ""
        title      = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", item.get("title", file_name)).strip()
        notion_url = item.get("notion_url", "")
 
        if link:
            msg = (
                f"🎙️ {title}\n\n"
                f"📥 Google Drive 播放連結：\n{link}\n\n"
                f"📖 Notion 完整摘要：\n{notion_url}"
            )
        else:
            msg = (
                f"🎙️ {title}\n\n"
                f"⚠️ 無法取得 Google Drive 連結（可能需要手動共享）\n\n"
                f"📖 Notion 完整摘要：\n{notion_url}"
            )
 
        _push(push_target, msg)
 
    except Exception as e:
        print(f"[BG] 選擇處理錯誤：{e}")
        _push(push_target, f"❌ 取得連結時發生錯誤：{str(e)[:100]}")
 
 
def _bg_handle_multi_selection(push_target: str, indices: list, items: list):
    """取得多個檔案的 Drive 連結"""
    try:
        _push(push_target, f"🔍 正在取得 {len(indices)} 個檔案的連結，請稍候...")
        for i, idx in enumerate(indices, 1):
            if idx < 0 or idx >= len(items):
                continue
            item = items[idx]
            file_name  = item.get("file_name", "")
            link       = get_drive_share_link(file_name) if file_name else ""
            title      = re.sub(r"\s*—\s*\d{4}/\d{2}/\d{2}$", "", item.get("title", file_name)).strip()
            notion_url = item.get("notion_url", "")
 
            if link:
                msg = (
                    f"🎙️ [{i}/{len(indices)}] {title}\n\n"
                    f"📥 Google Drive：\n{link}\n\n"
                    f"📖 Notion 摘要：\n{notion_url}"
                )
            else:
                msg = (
                    f"🎙️ [{i}/{len(indices)}] {title}\n\n"
                    f"⚠️ 無法取得 Drive 連結\n\n"
                    f"📖 Notion 摘要：\n{notion_url}"
                )
            _push(push_target, msg)
            time.sleep(0.5)
 
    except Exception as e:
        print(f"[BG] 多選處理錯誤：{e}")
        _push(push_target, f"❌ 取得連結時發生錯誤：{str(e)[:100]}")
 
 
# ════════════════════════════════════════════════════════════════════════════════
# LINE SDK 輔助
# ════════════════════════════════════════════════════════════════════════════════
 
def _reply(reply_token: str, text: str):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
 
def _push(user_id: str, text: str):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)],
            )
        )
 
 
# ════════════════════════════════════════════════════════════════════════════════
# Webhook 路由
# ════════════════════════════════════════════════════════════════════════════════
 
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"[Webhook] 處理錯誤：{e}")
    return "OK", 200
 
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "sessions": len(_sessions)}, 200
 
 
@handler.add(JoinEvent)
def handle_join(event):
    pass  # Bot 加入群組，靜默處理
 
@handler.add(MemberJoinedEvent)
def handle_member_joined(event):
    pass  # 成員加入，靜默處理
 
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id     = event.source.user_id
    text        = event.message.text.strip()
    reply_token = event.reply_token
 
    # 判斷來源：群組回覆給群組，個人回覆給個人
    source_type = event.source.type
    if source_type == "group":
        push_target = event.source.group_id
    elif source_type == "room":
        push_target = event.source.room_id
    else:
        push_target = user_id
 
    # ── 群組訊息：精確判斷是否 @ 到機器人本身 ───────────────────────────────
    if source_type == "group" or source_type == "room":
        mention     = getattr(event.message, "mention", None)
        mentionees  = getattr(mention, "mentionees", None) if mention else None
        bot_id      = _get_bot_user_id()
        bot_mentioned = False
 
        if mentionees:
            for m in mentionees:
                # 方法一：is_self（最準確）
                if getattr(m, "is_self", False):
                    bot_mentioned = True
                    break
                # 方法二：比對 bot 的 user_id
                if bot_id and getattr(m, "user_id", None) == bot_id:
                    bot_mentioned = True
                    break
        else:
            # 沒有 mention 資料時退回 @ 開頭判斷
            bot_mentioned = text.startswith("@")
 
        if not bot_mentioned:
            return
 
        # 去掉「@機器人名稱 」前綴
        text = re.sub(r"^@\S+\s*", "", text).strip()
        if not text:
            _reply(reply_token, "請在 @ 後面輸入您的問題，例如：\n@錄音檔推薦機器人 推薦我分享產品的錄音")
            return
 
    # ── 通關密碼驗證 ──────────────────────────────────────────────────────
    if BOT_PASSWORD:
        if user_id not in _authorized_users:
            if text == BOT_PASSWORD:
                _authorized_users.add(user_id)
                _reply(reply_token,
                    "✅ 驗證成功！歡迎使用錄音檔推薦機器人 🎙️\n\n"
                    "輸入「使用手冊」查看完整功能說明。"
                )
            else:
                _reply(reply_token,
                    "🔒 請輸入通關密碼才能使用此機器人。\n"
                    "（請向管理員索取密碼）"
                )
            return
 
    session = _get_session(user_id)
 
    # ── 使用者選擇推薦編號（1-5，支援多選）────────────────────────────────
    if session.get("state") == "selecting" and re.search(r"[1-5]", text):
        nums = re.findall(r"[1-5]", text)
        items = session.get("items", [])
        indices = list(dict.fromkeys([int(n) - 1 for n in nums]))
 
        if not indices:
            _reply(reply_token, "請輸入 1～5 之間的數字選擇錄音檔。")
            return
 
        _clear_session(user_id)
        # 同步取得 Drive 連結，使用免費 reply（不消耗月額度）
        _handle_selection_reply(reply_token, push_target, indices, items)
        return
 
    # ── 使用者從 /list 輸入編號取得 Drive 連結（支援多選）────────────────
    if session.get("state") == "listing" and re.search(r"\d", text):
        nums = re.findall(r"\d+", text)
        items = session.get("items", [])
        indices = [int(n) - 1 for n in nums if 1 <= int(n) <= len(items)]
 
        if not indices:
            _reply(reply_token, f"請輸入 1～{len(items)} 之間的數字。")
            return
 
        _clear_session(user_id)
        # 同步取得 Drive 連結，使用免費 reply（不消耗月額度）
        _handle_selection_reply(reply_token, push_target, indices, items)
        return
 
    # ── 特殊指令 ─────────────────────────────────────────────────────────────
    if text in ("取消", "重來", "cancel"):
        _clear_session(user_id)
        _reply(reply_token, "已取消，請輸入您的問題或需求，我來為您推薦合適的錄音檔 🎙️")
        return
 
    if text in ("說明", "help", "Help", "HELP"):
        _reply(reply_token,
            "🤖 錄音檔推薦機器人\n\n"
            "📌 使用方式：\n"
            "直接輸入您的問題或需求，例如：\n"
            "・「我不知道怎麼分享產品給朋友」\n"
            "・「推薦我跟業績提升相關的錄音」\n"
            "・「我想學如何管理時間」\n\n"
            "AI 會從資料庫中推薦 5 個最適合的錄音檔，\n"
            "回覆數字 1～5 即可取得播放連結。\n\n"
            "📋 指令：\n"
            "・/list — 列出所有錄音檔\n"
            "・/list 關鍵字 — 搜尋錄音檔名稱\n"
            "・使用手冊 — 完整功能說明\n"
            "・取消 — 重新開始"
        )
        return
 
    if text in ("使用手冊", "手冊", "manual"):
        _reply(reply_token,
            "🎙️ 錄音檔推薦機器人 使用手冊\n"
            "══════════════════════\n\n"
            "📖 這個機器人是做什麼的？\n"
            "從錄音檔資料庫中，根據你的問題或需求，用 AI 智慧推薦最適合的錄音檔，並直接提供 Notion 摘要頁面與 Google Drive 播放連結。\n\n"
            "══════════════════════\n\n"
            "📌 功能一：AI 推薦\n"
            "直接輸入你的問題或需求，Bot 會從資料庫掃描所有錄音檔，自動推薦 5 個最相關的，並同時附上摘要連結與音檔連結。\n\n"
            "範例問題：\n"
            "・我不知道怎麼開發新客戶\n"
            "・推薦跟業績提升相關的錄音\n"
            "・我想學習如何管理時間\n\n"
            "══════════════════════\n\n"
            "📋 功能二：列出清單\n"
            "輸入 /list 列出所有錄音檔（依資料夾分類）。\n"
            "也可以用 /list 關鍵字 搜尋特定錄音。\n\n"
            "輸入編號即可取得音檔連結：\n"
            "・單個：5\n"
            "・多個：1,3,5 或 1 3 5\n\n"
            "範例：\n"
            "・/list（列出全部）\n"
            "・/list 溝通（搜尋含「溝通」的錄音）\n\n"
            "══════════════════════\n\n"
            "🔤 其他指令\n"
            "・使用手冊 — 顯示此說明\n"
            "・說明 — 顯示簡易說明\n"
            "・取消 — 取消目前操作\n\n"
            "💡 群組中請先 @ 機器人再輸入指令"
        )
        return
 
    if text.startswith("/list"):
        keyword = text[5:].strip()
        # /list 同步執行並用 reply（免費，不佔月額度）
        _handle_list_reply(user_id, reply_token, push_target, keyword)
        return
 
    # ── 新問題 ────────────────────────────────────────────────────────────────
    _clear_session(user_id)
    _reply(reply_token, "🔍 正在為您分析推薦中，\n通常需要 15～30 秒，請稍候...")
    threading.Thread(target=_bg_handle_question, args=(user_id, push_target, text), daemon=True).start()
 
 
# ════════════════════════════════════════════════════════════════════════════════
# 啟動
# ════════════════════════════════════════════════════════════════════════════════
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 LINE Bot 啟動，監聽 port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
