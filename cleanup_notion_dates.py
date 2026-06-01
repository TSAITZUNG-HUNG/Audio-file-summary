#!/usr/bin/env python3
"""
清理 Notion 資料庫中標題含有日期（— 2026/XX/XX）的頁面
執行方式：python cleanup_notion_dates.py
"""
 
import os
import re
import requests
 
# ── 設定（從環境變數讀取，或直接填入）────────────────────────────────────────
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
 
# 如果沒有設定環境變數，直接填在這裡：
# NOTION_TOKEN       = "secret_..."
# NOTION_DATABASE_ID = "36d23f498590805bb482f88c02c8c737"
# ────────────────────────────────────────────────────────────────────────────────
 
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
 
DATE_PATTERN = re.compile(r"—\s*\d{4}/\d{2}/\d{2}")
 
 
def fetch_all_pages() -> list:
    """取得資料庫所有頁面"""
    pages = []
    has_more = True
    cursor = None
 
    while has_more:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
 
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=HEADERS, json=body, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
 
        for page in data.get("results", []):
            title_arr = page.get("properties", {}).get("Name", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_arr)
            pages.append({"id": page["id"], "title": title})
 
        has_more = data.get("has_more", False)
        cursor   = data.get("next_cursor")
 
    return pages
 
 
def archive_page(page_id: str):
    """封存（刪除）Notion 頁面"""
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"archived": True},
        timeout=10
    )
    resp.raise_for_status()
 
 
def main():
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("❌ 請先設定 NOTION_TOKEN 和 NOTION_DATABASE_ID")
        return
 
    print("📋 讀取 Notion 資料庫...")
    pages = fetch_all_pages()
    print(f"   共 {len(pages)} 個頁面")
 
    # 找出標題含有日期的頁面
    to_delete = [p for p in pages if DATE_PATTERN.search(p["title"])]
    print(f"\n🗑️  找到 {len(to_delete)} 個含日期的頁面：")
    for p in to_delete:
        print(f"   - {p['title']}")
 
    if not to_delete:
        print("\n✅ 沒有需要清理的頁面！")
        return
 
    import sys
    auto = "--auto" in sys.argv
    if not auto:
        confirm = input(f"\n確定要刪除這 {len(to_delete)} 個頁面嗎？(y/N) ").strip().lower()
        if confirm != "y":
            print("取消。")
            return
 
    print("\n開始刪除...")
    success = 0
    for p in to_delete:
        try:
            archive_page(p["id"])
            print(f"   ✅ 已刪除：{p['title']}")
            success += 1
        except Exception as e:
            print(f"   ❌ 失敗：{p['title']} — {e}")
 
    print(f"\n🎉 完成！成功刪除 {success} 個頁面。")
 
 
if __name__ == "__main__":
    main()
