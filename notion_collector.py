import os
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from notion_client import Client

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

def get_target_yesterday():
    now_kst = datetime.now(KST)
    yesterday = (now_kst - timedelta(days=1)).date()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = weekdays[yesterday.weekday()]
    return yesterday, day_str

def extract_entry_date(entry):
    date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if date_struct:
        dt_utc = datetime(*date_struct[:6], tzinfo=zoneinfo.ZoneInfo("UTC"))
        return dt_utc.astimezone(KST).date()

    raw_date = entry.get("published") or entry.get("updated") or entry.get("pubDate")
    if raw_date:
        try:
            dt = date_parser.parse(raw_date)
            return dt.astimezone(KST).date() if dt.tzinfo else dt.date()
        except Exception:
            return None
    return None

def main():
    yesterday, day_str = get_target_yesterday()
    yesterday_fmt = yesterday.strftime("%Y.%m.%d")
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===")

    # 로컬 feed.xml 우선 탐색
    feed_source = "feed.xml" if os.path.exists("feed.xml") else FEED_URL
    feed = feedparser.parse(feed_source)
    print(f"[*] 총 피드 항목 수: {len(feed.entries)}개")

    target_entries = []
    for entry in feed.entries:
        item_date = extract_entry_date(entry)
        
        # 디버깅 출력: 상위 3개 항목의 날짜 파싱 결과 확인용
        if len(target_entries) < 3 and item_date:
            print(f"[샘플 로그] 제목: {entry.get('title')[:15]}... -> 파싱된 날짜: {item_date}")

        if item_date == yesterday:
            target_entries.append({
                "title": entry.get("title", "제목 없음"),
                "link": entry.get("link", ""),
                "department": entry.get("author", "담당부처 미상")
            })

    print(f"[*] 최종 매칭된 어제자 항목: {len(target_entries)}개")

    if not target_entries:
        print("[!] 대상 날짜와 일치하는 기사가 없습니다. 워크플로우를 안전하게 종료합니다.")
        return

    # 상위 페이지 생성
    parent_title = f"보도자료_{yesterday_fmt}.({day_str})"
    clean_parent_id = PARENT_PAGE_ID.replace("-", "").strip()
    
    parent_page = notion.pages.create(
        parent={"page_id": clean_parent_id},
        properties={"title": [{"text": {"content": parent_title}}]}
    )
    parent_id = parent_page["id"]
    print(f"[+] 노션 상위 페이지 생성 성공: {parent_title}")

    # 하위 페이지 생성 반복문
    for idx, item in enumerate(target_entries, start=1):
        child_title = f"[{idx}] [{item['department']}] {item['title']}"
        notion.pages.create(
            parent={"page_id": parent_id},
            properties={"title": [{"text": {"content": child_title}}]},
            children=[
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {"text": {"content": f"담당부처: {item['department']} | 원문: "}},
                            {"text": {"content": "링크 바로가기", "link": {"url": item['link']}}}
                        ],
                        "icon": {"emoji": "🔗"}
                    }
                }
            ]
        )
        print(f"  -> 하위 페이지 등록 완료: {child_title}")

if __name__ == "__main__":
    main()
