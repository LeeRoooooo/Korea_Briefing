import os
import re
import html
import time
from datetime import datetime, timedelta
import zoneinfo
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.sync_api import sync_playwright
from notion_client import Client

# -------------------------------------------------------------
# 1. 환경 변수 및 노션 클라이언트 설정
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
# 2. 본문 정밀 정제 로직
# -------------------------------------------------------------
def clean_html_to_text(element):
    """HTML 요소에서 불필요한 영역을 제거하고 줄바꿈을 살려 텍스트 반환"""
    if not element:
        return ""

    # 불필요한 태그 완전 제거
    for tag in element.find_all(class_=["remark_briefing", "law_copy_wrap", "filedown", "kogl", "btn"]):
        tag.decompose()
    for tag in element.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # 줄바꿈 태그 변환
    for br in element.find_all("br"):
        br.replace_with("\n")
    for p in element.find_all(["p", "div"]):
        p.append("\n")

    raw_text = html.unescape(element.get_text())

    # 공백 정리 및 잔여 전재 문구 필터링
    cleaned_lines = []
    for line in raw_text.splitlines():
        line_str = re.sub(r"[ \t]+", " ", line).strip()
        if "보도자료를 전재하여 제공함을 알려드립니다" in line_str:
            continue
        if line_str:
            cleaned_lines.append(line_str)

    return "\n\n".join(cleaned_lines)

def fetch_content_with_mobile_browser(page, url, target_dept, fallback_desc=""):
    """
    Referer 우회 및 방화벽 완화 딜레이를 적용하여 상세 본문 추출
    """
    try:
        # 봇 탐지 회피를 위한 사람형 탐색 간격 (2.0 ~ 2.5초 대기)
        time.sleep(2.2)

        # 목록 페이지에서 클릭해 들어온 것처럼 헤더 주입
        page.set_extra_http_headers({
            "Referer": "https://www.korea.kr/briefing/pressReleaseList.do",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"
        })

        # 네트워크 수신 대기 (최대 25초)
        response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
        
        # DOM 렌더링 완료를 위해 1초 대기
        page.wait_for_timeout(1000)

        html_content = page.content()
        soup = BeautifulSoup(html_content, "html.parser")

        view_containers = soup.find_all("div", class_="view_cont")
        target_container = None

        clean_dept = target_dept.strip() if target_dept else ""
        if clean_dept and clean_dept != "담당부처 미상":
            for vc in view_containers:
                data_title = vc.get("data-title", "").strip()
                if clean_dept in data_title or data_title in clean_dept:
                    target_container = vc
                    break

        if not target_container and view_containers:
            target_container = max(view_containers, key=lambda c: len(c.get_text(strip=True)))

        if not target_container:
            target_container = soup.find("div", class_="article_body")

        if target_container:
            text_content = clean_html_to_text(target_container)
            if len(text_content.strip()) >= 30:
                return text_content

    except Exception as e:
        print(f"      [!] 브라우저 접근 예외 ({e}) -> 피드 요약본 자동 대체")

    # 방화벽에 막혀 ERR_EMPTY_RESPONSE가 발생해도 피드 요약본으로 무조건 적재
    if fallback_desc and len(fallback_desc.strip()) > 10:
        desc_soup = BeautifulSoup(fallback_desc, "html.parser")
        desc_text = desc_soup.get_text(separator="\n\n").strip()
        if len(desc_text) >= 20:
            return f"[피드 발췌 요약본]\n\n{desc_text}"

    return "상세 보도자료 본문을 불러올 수 없습니다. 상단 원문 링크를 확인해 주세요."

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
# 3. 메인 파이프라인
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
                "link": entry.get("link", ""),
                "description": entry.get("description", "")
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

    # Playwright 모바일 에뮬레이터 가동
    print("[*] Playwright 모바일 에뮬레이터(iPhone 14) 기동 중...")
    with sync_playwright() as p:
        device_profile = p.devices["iPhone 14"]
        browser = p.chromium.launch(headless=True)
        
        # 목록 페이지를 먼저 한 번 방문하여 정상 쿠키와 세션 확보
        context = browser.new_context(
            **device_profile,
            locale="ko-KR",
            timezone_id="Asia/Seoul"
        )
        page = context.new_page()

        try:
            print("[*] 방화벽 사전 인증을 위해 정책브리핑 메인 세션 로딩 중...")
            page.goto("https://www.korea.kr/briefing/pressReleaseList.do", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[!] 메인 페이지 세션 생성 경고 ({e}) - 수집을 계속 진행합니다.")

        for idx, item in enumerate(target_entries, start=1):
            child_title = f"[{idx:02d}] [{item['dept']}] {item['title']}"
            print(f"  -> [{idx}/{len(target_entries)}] 모바일 본문 수집 중: [{item['dept']}] {item['title'][:20]}...")
            
            # 모바일 페이지 본문 추출 (실패 시 피드 요약본 자동 적재)
            body_text = fetch_content_with_mobile_browser(
                page=page,
                url=item["link"],
                target_dept=item["dept"],
                fallback_desc=item.get("description", "")
            )

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

            paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
            for p_chunk in paragraphs[:30]:
                chunks = [p_chunk[i:i+1500] for i in range(0, len(p_chunk), 1500)]
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

        browser.close()

    print("=== [완료] 모든 보도자료 본문 추출 및 노션 적재 성공! ===")

if __name__ == "__main__":
    main()
