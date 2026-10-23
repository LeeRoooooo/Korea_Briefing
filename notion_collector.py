import os
import re
import time
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from notion_client import Client

# 환경 변수
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

def get_target_yesterday():
    """한국 시간(KST) 기준 전일자 날짜 객체 및 요일 반환"""
    now_kst = datetime.now(KST)
    yesterday = (now_kst - timedelta(days=1)).date()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = weekdays[yesterday.weekday()]
    return yesterday, day_str

def extract_entry_date(entry):
    """RSS 항목의 날짜를 KST date 객체로 파싱"""
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

def extract_department_and_title(raw_title):
    """제목의 [부처명]과 순수 제목 분리"""
    match = re.search(r"^\[(.*?)\]", raw_title.strip())
    if match:
        dept = match.group(1).strip()
        clean_title = re.sub(r"^\[.*?\]\s*", "", raw_title).strip()
        return dept, clean_title
    return "담당부처 미상", raw_title.strip()

def fetch_mobile_article_content(page_url):
    """
    모바일 헤더로 페이지에 접근하여 <div class="view_cont"> 영역의 본문 텍스트 정밀 추출
    """
    # 모바일 기기(아이폰 사파리)로 위장하여 모바일 뷰 렌더링 유도
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9"
    }
    
    try:
        res = requests.get(page_url, timeout=12, headers=mobile_headers)
        if res.status_code != 200:
            return "상세 페이지에 접속할 수 없습니다."

        soup = BeautifulSoup(res.text, "html.parser")

        # 1. 대상 컨테이너(.view_cont 또는 .article_body) 정밀 타격
        cont_div = soup.select_one(".view_cont") or soup.select_one(".article_body")
        
        if not cont_div:
            return "본문 영역(.view_cont)을 찾을 수 없습니다. 원문 링크를 확인해 주세요."

        # 2. 불필요한 공통 태그(저작권 안내, 재배포 문구, 스크립트 등) 사전 제거
        for tag in cont_div.select("script, style, .remark_briefing, .copyright, noscript"):
            tag.decompose()

        # 3. <br/> 태그와 <div> 태그를 줄바꿈(\n)으로 치환
        for br in cont_div.find_all("br"):
            br.replace_with("\n")

        # 4. 텍스트 추출 및 공백/불필요한 공백 행 정리
        raw_text = cont_div.get_text()
        lines = [line.strip() for line in raw_text.splitlines()]
        
        # 연속된 빈 줄을 하나로 압축하고 본문 복원
        cleaned_paragraphs = []
        current_chunk = []

        for line in lines:
            if line:
                current_chunk.append(line)
            elif current_chunk:
                cleaned_paragraphs.append(" ".join(current_chunk))
                current_chunk = []
        if current_chunk:
            cleaned_paragraphs.append(" ".join(current_chunk))

        final_content = "\n\n".join(cleaned_paragraphs)
        return final_content if len(final_content) > 30 else "본문 내용이 비어 있습니다."

    except Exception as e:
        return f"본문 파싱 오류 발생: {e}"

def get_database_title_property_name(db_id):
    """노션 데이터베이스의 제목 컬럼 이름 자동 조회"""
    try:
        db_info = notion.databases.retrieve(database_id=db_id)
        for prop_name, prop_data in db_info.get("properties", {}).items():
            if prop_data.get("type") == "title":
                return prop_name
    except Exception:
        pass
    return "이름"

def main():
    yesterday, day_str = get_target_yesterday()
    yesterday_fmt = yesterday.strftime("%Y.%m.%d")
    
    print(f"=== [수집 시작] 대상일: {yesterday_fmt} ({day_str}요일) ===")

    # 피드 로드
    feed_source = "feed.xml" if os.path.exists("feed.xml") else FEED_URL
    feed = feedparser.parse(feed_source)
    print(f"[*] 총 피드 항목 수: {len(feed.entries)}개")

    target_entries = []
    for entry in feed.entries:
        if extract_entry_date(entry) == yesterday:
            raw_title = entry.get("title", "제목 없음")
            dept, clean_title = extract_department_and_title(raw_title)
            
            target_entries.append({
                "dept": dept,
                "title": clean_title,
                "link": entry.get("link", "")
            })

    print(f"[*] 매칭된 어제자 보도자료: {len(target_entries)}개")

    if not target_entries:
        print("[!] 대상 날짜 기사가 없어 종료합니다.")
        return

    # 노션 데이터베이스 상위 페이지 생성
    clean_db_id = DATABASE_ID.replace("-", "").strip()
    title_prop_name = get_database_title_property_name(clean_db_id)
    parent_title = f"보도자료_{yesterday_fmt}.({day_str})"

    parent_page = notion.pages.create(
        parent={"database_id": clean_db_id},
        properties={
            title_prop_name: {"title": [{"text": {"content": parent_title}}]}
        }
    )
    parent_page_id = parent_page["id"]
    print(f"[+] 노션 상위 항목 생성 완료: {parent_title}")

    # 하위 보도자료 세부 페이지 등록
    for idx, item in enumerate(target_entries, start=1):
        child_title = f"[{idx:02d}] [{item['dept']}] {item['title']}"
        print(f"  -> [{idx}/{len(target_entries)}] 모바일 본문 수집: {item['dept']} - {item['title'][:20]}...")
        
        body_text = fetch_mobile_article_content(item["link"])

        # 노션 블록 구성
        content_blocks = [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [
                        {"text": {"content": f"담당부처: {item['dept']} | "}},
                        {"text": {"content": "원문 링크 바로가기", "link": {"url": item['link']}}}
                    ],
                    "icon": {"emoji": "🏛️"}
                }
            },
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": "보도자료 상세 본문"}}]}
            }
        ]

        # 단락별로 노션 블록 생성 (노션 2,000자 제한 방어)
        paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
        for p in paragraphs[:25]:  # 주요 단락 최대 25개
            chunks = [p[i:i+1500] for i in range(0, len(p), 1500)]
            for chunk in chunks:
                content_blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": chunk}}]}
                })

        notion.pages.create(
            parent={"page_id": parent_page_id},
            properties={"title": [{"text": {"content": child_title}}]},
            children=content_blocks
        )
        time.sleep(0.35)

    print("=== [완료] 모바일 본문 크롤링 및 노션 적재가 완벽히 끝났습니다! ===")

if __name__ == "__main__":
    main()
