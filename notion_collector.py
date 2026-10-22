import os
import time
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from notion_client import Client

# 1. 환경 변수 및 설정
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")  # DB의 32자리 ID
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

def get_target_yesterday():
    """한국 시간(KST) 기준 정확한 전일자 날짜 객체 및 요일 문자열 생성"""
    now_kst = datetime.now(KST)
    yesterday = (now_kst - timedelta(days=1)).date()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = weekdays[yesterday.weekday()]
    return yesterday, day_str

def extract_entry_date(entry):
    """RSS 항목의 날짜를 파싱하여 KST 기준 date 객체로 반환"""
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

def parse_detail_content(url):
    """보도자료 세부 원문 본문 텍스트 추출 (스크립트/광고/첨부 제거)"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, timeout=12, headers=headers)
        if res.status_code != 200:
            return "본문 링크에 연결할 수 없습니다."
        
        soup = BeautifulSoup(res.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "form"]):
            tag.decompose()
            
        paragraphs = [p.get_text(strip=True) for p in soup.find_all(["p", "div"]) if len(p.get_text(strip=True)) > 20]
        # 중복 문장 필터링 및 병합
        unique_paras = list(dict.fromkeys(paragraphs))
        content = "\n\n".join(unique_paras[:10])  # 주요 상위 단락 추출
        return content[:1800] if content else "상세 본문 텍스트를 파싱할 수 없습니다."
    except Exception as e:
        return f"본문 파싱 오류: {e}"

def get_database_title_property_name(db_id):
    """데이터베이스의 title 속성명 자동 판별 ('이름' 또는 'Name')"""
    try:
        db_info = notion.databases.retrieve(database_id=db_id)
        for prop_name, prop_data in db_info.get("properties", {}).items():
            if prop_data.get("type") == "title":
                return prop_name
    except Exception as e:
        print(f"[!] 데이터베이스 스키마 조회 참고: {e}")
    return "이름"  # 기본 폴백

def main():
    yesterday, day_str = get_target_yesterday()
    yesterday_fmt = yesterday.strftime("%Y.%m.%d")
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===")

    # 1. 피드 파싱 (로컬 feed.xml 우선, 없으면 Pages URL)
    feed_source = "feed.xml" if os.path.exists("feed.xml") else FEED_URL
    feed = feedparser.parse(feed_source)
    print(f"[*] 총 피드 항목 수: {len(feed.entries)}개")

    target_entries = []
    for entry in feed.entries:
        item_date = extract_entry_date(entry)
        if item_date == yesterday:
            target_entries.append({
                "title": entry.get("title", "제목 없음"),
                "link": entry.get("link", ""),
                "department": entry.get("author", "담당부처 미상")
            })

    print(f"[*] 필터링된 어제자 보도자료 항목: {len(target_entries)}개")

    if not target_entries:
        print("[!] 대상 날짜와 일치하는 기사가 없습니다. 종료합니다.")
        return

    # 2. 데이터베이스 ID 정제
    clean_db_id = DATABASE_ID.replace("-", "").strip()

    # 3. 데이터베이스 내 상위 일자별 페이지 생성
    title_prop_name = get_database_title_property_name(clean_db_id)
    parent_title = f"보도자료_{yesterday_fmt}.({day_str})"
    
    print(f"[*] 데이터베이스({clean_db_id})에 상위 항목 생성 중...")
    parent_page = notion.pages.create(
        parent={"database_id": clean_db_id},
        properties={
            title_prop_name: {
                "title": [{"text": {"content": parent_title}}]
            }
        }
    )
    parent_page_id = parent_page["id"]
    print(f"[+] 노션 상위 항목 생성 완료: {parent_title} (ID: {parent_page_id})")

    # 4. 상위 페이지 아래에 각 보도자료별 하위 페이지 등록
    for idx, item in enumerate(target_entries, start=1):
        child_title = f"[{idx:02d}] [{item['department']}] {item['title']}"
        detail_text = parse_detail_content(item["link"])

        notion.pages.create(
            parent={"page_id": parent_page_id},  # 상위 페이지 아래에 중첩 페이지로 생성
            properties={
                "title": [{"text": {"content": child_title}}]
            },
            children=[
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {"text": {"content": f"담당부처: {item['department']} | "}},
                            {"text": {"content": "원문 링크 바로가기", "link": {"url": item['link']}}}
                        ],
                        "icon": {"emoji": "📰"}
                    }
                },
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"text": {"content": "보도자료 상세 내용"}}]
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
        print(f"  -> [{idx}/{len(target_entries)}] 하위 세부 페이지 등록 완료: {item['title'][:20]}...")
        time.sleep(0.35)  # 노션 API Rate Limit 방지용 딜레이

    print("=== [완료] 모든 보도자료 적재가 성공적으로 끝났습니다! ===")

if __name__ == "__main__":
    main()
