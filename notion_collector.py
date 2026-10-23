import os
import re
import html
import time
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from notion_client import Client

# -------------------------------------------------------------
# 1. 환경 변수 및 클라이언트 설정
# -------------------------------------------------------------
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
    """RSS 항목 날짜 파싱"""
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

# -------------------------------------------------------------
# 2. 본문 텍스트 정밀 파싱 로직
# -------------------------------------------------------------
def clean_html_to_text(element):
    """HTML 요소에서 전재 문구/저작권을 제거하고 줄바꿈을 완벽히 살려 순수 텍스트 반환"""
    if not element:
        return ""

    # 1. 전재 안내문구(remark_briefing), 저작권(law_copy_wrap), 첨부파일, 스크립트 등 완전 제거
    for tag in element.find_all(class_=["remark_briefing", "law_copy_wrap", "filedown", "kogl", "btn"]):
        tag.decompose()
    for tag in element.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # 2. 줄바꿈 태그들을 실제 개행(\n)으로 변환
    for br in element.find_all("br"):
        br.replace_with("\n")
    for p in element.find_all(["p", "div"]):
        p.append("\n")

    # 3. HTML 엔티티(&middot;, &#x3147;, &nbsp; 등) 디코딩
    raw_text = html.unescape(element.get_text())

    # 4. 빈 줄 정리 및 문단 정제 (전재 문구 잔여물 2차 검증 필터)
    cleaned_lines = []
    for line in raw_text.splitlines():
        line_str = re.sub(r"[ \t]+", " ", line).strip()
        if "보도자료를 전재하여 제공함을 알려드립니다" in line_str:
            continue
        if line_str:
            cleaned_lines.append(line_str)

    return "\n\n".join(cleaned_lines)

def fetch_article_content(page_url, target_dept):
    """
    모바일 헤더로 요청 후, 실제 본문 텍스트가 담긴 view_cont 영역을 정밀 타격하여 추출
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9"
    }

    try:
        res = requests.get(page_url, timeout=15, headers=headers)
        if res.status_code != 200:
            return "상세 페이지에 접속할 수 없습니다 (HTTP 에러)."

        soup = BeautifulSoup(res.text, "html.parser")

        # 1. 모든 view_cont 태그 수집
        view_containers = soup.find_all("div", class_="view_cont")
        target_container = None

        # 2. 1순위: data-title 속성에 부처명이 포함된 컨테이너 탐색
        clean_dept = target_dept.strip() if target_dept else ""
        if clean_dept and clean_dept != "담당부처 미상":
            for vc in view_containers:
                data_title = vc.get("data-title", "").strip()
                if clean_dept in data_title or data_title in clean_dept:
                    target_container = vc
                    break

        # 3. 2순위: 부처 매칭 실패 시, 글자 수가 가장 긴 view_cont 자동 선택 (알맹이 자동 포착)
        if not target_container and view_containers:
            target_container = max(view_containers, key=lambda c: len(c.get_text(strip=True)))

        # 4. 3순위: article_body 폴백
        if not target_container:
            target_container = soup.find("div", class_="article_body")

        if not target_container:
            return "본문 영역(view_cont)을 찾을 수 없습니다. 원문 링크를 확인해 주세요."

        # 순수 텍스트 정제
        text_content = clean_html_to_text(target_container)

        if len(text_content.strip()) < 30:
            return "본문 내용이 너무 짧거나 비어 있습니다. 원문 링크를 확인해 주세요."

        return text_content

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

# -------------------------------------------------------------
# 3. 메인 실행 루틴
# -------------------------------------------------------------
def main():
    yesterday, day_str = get_target_yesterday()
    yesterday_fmt = yesterday.strftime("%Y.%m.%d")
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===")

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
        print("[!] 대상 날짜 기사가 없어 작업을 안전하게 종료합니다.")
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
        print(f"  -> [{idx}/{len(target_entries)}] 본문 수집 중: [{item['dept']}] {item['title'][:20]}...")
        
        body_text = fetch_article_content(item["link"], item["dept"])

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
                "heading_3": {"rich_text": [{"text": {"content": "보도자료 본문"}}]}
            }
        ]

        # 단락별 분할 적재 (노션 2,000자 블록 제한 방어)
        paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
        for p in paragraphs[:30]:
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
        time.sleep(0.35)  # API Rate limit 방지

    print("=== [완료] 모든 보도자료 본문 추출 및 노션 적재 성공! ===")

if __name__ == "__main__":
    main()
