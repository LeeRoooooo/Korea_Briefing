import os
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from notion_client import Client

# 1. 환경 변수 및 클라이언트 설정
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
PARENT_PAGE_ID = os.environ["NOTION_PARENT_PAGE_ID"]
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

def get_yesterday_kst():
    yesterday = datetime.now(KST) - timedelta(days=1)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = weekdays[yesterday.weekday()]
    return yesterday.strftime("%Y.%m.%d"), day_str

def parse_detail_content(url):
    """세부 보도자료 본문 추출 (텍스트만)"""
    try:
        res = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            return "본문을 불러올 수 없습니다."
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 스크립트, 스타일, 첨부파일 영역 제거
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
            
        paragraphs = [p.get_text(strip=True) for p in soup.find_all(["p", "div"]) if p.get_text(strip=True)]
        # 중복 제거 및 적정 길이 병합
        content = "\n\n".join(dict.fromkeys(paragraphs))
        return content[:1500] if content else "본문 내용이 비어 있습니다."
    except Exception as e:
        return f"본문 파싱 오류: {e}"

def main():
    date_str, day_str = get_yesterday_kst()
    target_date_prefix = date_str.replace(".", "-") # YYYY-MM-DD
    
    feed = feedparser.parse(FEED_URL)
    target_entries = []
    
    for entry in feed.entries:
        # 발행일 기준 전일자 필터링
        published = entry.get("published", "") or entry.get("updated", "")
        if target_date_prefix in published or date_str in published:
            title = entry.get("title", "제목 없음")
            link = entry.get("link", "")
            department = entry.get("author", "담당부처 미상")
            target_entries.append({
                "title": title,
                "link": link,
                "department": department
            })

    if not target_entries:
        print(f"[{date_str}] 전일자 보도자료 데이터가 없습니다.")
        return

    # 2. 상위 페이지 생성: '보도자료_0000.00.00.(요일)'
    parent_title = f"보도자료_{date_str}.({day_str})"
    parent_page = notion.pages.create(
        parent={"page_id": PARENT_PAGE_ID},
        properties={
            "title": [{"text": {"content": parent_title}}]
        }
    )
    parent_id = parent_page["id"]
    print(f"상위 페이지 생성 완료: {parent_title}")

    # 3. 각 보도자료 하위 페이지 생성 및 요약
    for idx, item in enumerate(target_entries, start=1):
        detail_text = parse_detail_content(item["link"])
        child_title = f"[{idx}] [{item['department']}] {item['title']}"
        
        # 하위 페이지 생성
        notion.pages.create(
            parent={"page_id": parent_id},
            properties={
                "title": [{"text": {"content": child_title}}]
            },
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
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"text": {"content": detail_text}}]
                    }
                }
            ]
        )
        print(f"하위 페이지 등록: {child_title}")

if __name__ == "__main__":
    main()
