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
from linebot.v3.webhooks import MessageEvent, TextMessageContent
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
 
def _fetch_page_summary(page_id: str) -> str:
    try:
        resp = req_lib.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=30",
            headers=_notion_headers(), timeout=10
        )
        if resp.status_code != 200:
            return ""
        blocks = resp.json().get("results", [])
        lines = []
        for block in blocks[:25]:
            btype = block.get("type", "")
            rt = block.get(btype, {}).get("rich_text", [])
            text = "".join(r.get("plain_text", "") for r in rt).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)[:1500]
    except Exception:
        return ""
 
def fetch_all_summaries(max_pages: int = 200) -> list:
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
 
            summary_text = _fetch_page_summary(page["id"])
 
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
 
def recommend_transcripts(question: str, summaries: list) -> list:
    index_lines = []
    for i, s in enumerate(summaries):
        kw      = "、".join(s["keywords"][:5]) if s["keywords"] else "無"
        dur     = f"{s['duration_min']:.0f}分鐘" if s["duration_min"] else "不明"
        snippet = s["summary_text"][:300].replace("\n", " ")
        index_lines.append(
            f"[{i+1}] 《{s['title']}》\n"
            f"  關鍵字：{kw}　時長：{dur}\n"
            f"  內容摘要：{snippet}"
        )
 
    # 只取前 60 筆，避免超過 Groq TPM 限制
    index_lines = index_lines[:60]
    context = "\n\n".join(index_lines)
    if len(context) > 6000:
        context = context[:6000] + "\n...(以下省略)"
 
    prompt = f"""使用者的問題／需求：
「{question}」
 
以下是錄音檔資料庫（共 {len(summaries)} 個）：
 
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
        if 0 <= idx < len(summaries):
            item = summaries[idx].copy()
            item["reason"] = reasons.get(rank, "")
            result.append(item)
 
    return result
 
 
# ════════════════════════════════════════════════════════════════════════════════
# LINE 訊息格式化
# ════════════════════════════════════════════════════════════════════════════════
 
_CIRCLE = ["①", "②", "③", "④", "⑤"]
 
def format_recommendations(items: list, question: str) -> str:
    lines = [
        f'🎙️ 根據您的需求：「{question[:50]}」\n我推薦以下 5 個錄音檔：\n',
    ]
    for i, item in enumerate(items):
        dur = f"{item['duration_min']:.0f}分鐘" if item["duration_min"] else ""
        kw  = " #".join(item["keywords"][:3]) if item["keywords"] else ""
        kw  = f"#{kw}" if kw else ""
        lines.append(
            f"{_CIRCLE[i]} {item['title']}\n"
            f"💡 {item['reason']}\n"
            f"⏱ {dur}　{kw}\n"
            f"📖 摘要：{item['notion_url']}\n"
        )
    lines.append("─────────────────\n請回覆數字 1～5 選擇想聽的錄音檔，\n我會傳送 Google Drive 連結給您！")
    return "\n".join(lines)
 
 
# ════════════════════════════════════════════════════════════════════════════════
# 背景任務處理
# ════════════════════════════════════════════════════════════════════════════════
 
def _bg_handle_question(user_id: str, question: str):
    try:
        summaries = fetch_all_summaries()
        if not summaries:
            _push(user_id, "⚠️ Notion 資料庫目前沒有摘要，請先執行錄音檔摘要機器人產生摘要。")
            return
 
        items = recommend_transcripts(question, summaries)
        if not items:
            _push(user_id, "😅 找不到合適的推薦，請換個方式描述您的需求，例如：\n「我想學習如何開發客戶」\n「推薦我關於時間管理的錄音」")
            return
 
        _set_session(user_id, {"state": "selecting", "items": items, "question": question})
        _push(user_id, format_recommendations(items, question))
 
    except Exception as e:
        print(f"[BG] 問題處理錯誤：{e}")
        _push(user_id, f"❌ 處理時發生錯誤，請稍後再試。\n（{str(e)[:100]}）")
 
 
def _bg_handle_selection(user_id: str, idx: int, item: dict):
    try:
        file_name  = item.get("file_name", "")
        link       = get_drive_share_link(file_name) if file_name else ""
        title      = item.get("title", file_name)
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
 
        _push(user_id, msg)
 
    except Exception as e:
        print(f"[BG] 選擇處理錯誤：{e}")
        _push(user_id, f"❌ 取得連結時發生錯誤：{str(e)[:100]}")
 
 
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
 
 
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id     = event.source.user_id
    text        = event.message.text.strip()
    reply_token = event.reply_token
 
    session = _get_session(user_id)
 
    # ── 使用者選擇推薦編號 ──────────────────────────────────────────────────
    if session.get("state") == "selecting" and re.match(r"^[1-5]$", text):
        idx   = int(text) - 1
        items = session.get("items", [])
 
        if idx >= len(items):
            _reply(reply_token, "請輸入 1～5 之間的數字選擇錄音檔。")
            return
 
        item = items[idx]
        _reply(reply_token, f"🔍 正在取得「{item['title'][:30]}」的連結，請稍候...")
        _clear_session(user_id)
        threading.Thread(target=_bg_handle_selection, args=(user_id, idx, item), daemon=True).start()
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
            "輸入「取消」可重新開始。"
        )
        return
 
    # ── 新問題 ────────────────────────────────────────────────────────────────
    _clear_session(user_id)
    _reply(reply_token, "🔍 正在為您分析推薦中，\n通常需要 15～30 秒，請稍候...")
    threading.Thread(target=_bg_handle_question, args=(user_id, text), daemon=True).start()
 
 
# ════════════════════════════════════════════════════════════════════════════════
# 啟動
# ════════════════════════════════════════════════════════════════════════════════
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 LINE Bot 啟動，監聽 port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
