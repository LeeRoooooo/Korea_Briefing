import os
import re
import html
import time
from datetime import datetime, timedelta
import zoneinfo
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from notion_client import Client

# -------------------------------------------------------------
# 1. 환경 변수 및 노션 클라이언트 설정
# -------------------------------------------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

# -------------------------------------------------------------
# 2. 방화벽 차단 회피형 HTTP 세션 구성 (Retry + Backoff)
# -------------------------------------------------------------
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False
)
adapter = HTTPAdapter(max_retries=retries)
session.mount("https://", adapter)
session.mount("http://", adapter)

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
# 3. 본문 정밀 정제 및 방화벽 대응 웹 스크레이퍼
# -------------------------------------------------------------
def clean_html_to_text(element):
    """HTML 요소에서 전재 문구/저작권을 제거하고 줄바꿈을 완벽히 살려 순수 텍스트 반환"""
    if not element:
        return ""

    # 전재 안내문구, 저작권, 첨부파일 상자, 스크립트 등 잡음 태그 완전 제거
    for tag in element.find_all(class_=["remark_briefing", "law_copy_wrap", "filedown", "kogl", "btn"]):
        tag.decompose()
    for tag in element.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # 줄바꿈 태그들을 실제 개행(\n)으로 변환
    for br in element.find_all("br"):
        br.replace_with("\n")
    for p in element.find_all(["p", "div"]):
        p.append("\n")

    # HTML 엔티티(&middot;, &#x3147;, &nbsp; 등) 디코딩
    raw_text = html.unescape(element.get_text())

    # 빈 줄 정리 및 문단 정제 (전재 문구 잔여물 2차 검증 필터)
    cleaned_lines = []
    for line in raw_text.splitlines():
        line_str = re.sub(r"[ \t]+", " ", line).strip()
        if "보도자료를 전재하여 제공함을 알려드립니다" in line_str:
            continue
        if line_str:
            cleaned_lines.append(line_str)

    return "\n\n".join(cleaned_lines)

def fetch_article_content(page_url, target_dept, fallback_description=""):
    """
    브라우저 표준 세션으로 요청하여 view_cont 영역을 정밀 추출하되,
    방화벽 차단(RemoteDisconnected 등) 발생 시 feed.xml의 요약문으로 안전하게 전환
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

    try:
        # 방화벽 연속 타격 방지 딜레이
        time.sleep(0.4)
        
        res = session.get(page_url, timeout=12, headers=headers)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")

            # 1. view_cont 컨테이너 탐색
            view_containers = soup.find_all("div", class_="view_cont")
            target_container = None

            clean_dept = target_dept.strip() if target_dept else ""
            if clean_dept and clean_dept != "담당부처 미상":
                for vc in view_containers:
                    data_title = vc.get("data-title", "").strip()
                    if clean_dept in data_title or data_title in clean_dept:
                        target_container = vc
                        break

            # 2. 매칭 실패 시 글자 수가 가장 긴 view_cont 컨테이너 자동 선택
            if not target_container and view_containers:
                target_container = max(view_containers, key=lambda c: len(c.get_text(strip=True)))

            # 3. article_body 폴백
            if not target_container:
                target_container = soup.find("div", class_="article_body")

            if target_container:
                text_content = clean_html_to_text(target_container)
                if len(text_content.strip()) >= 30:
                    return text_content

    except Exception as e:
        print(f"      [!] 웹 본문 스크레이핑 예외 ({e}) -> 피드 백업 본문으로 전환")

    # 4. 방화벽 차단 또는 빈 페이지일 때 feed.xml의 description 본문 활용
    if fallback_description and len(fallback_description.strip()) > 10:
        desc_soup = BeautifulSoup(fallback_description, "html.parser")
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
# 4. 메인 파이프라인
# -------------------------------------------------------------
def main():
    yesterday, day_str = get_target_yesterday()
    yesterday_fmt = yesterday.strftime("%Y.%m.%d")
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===")

    # 갓 생성된 로컬 feed.xml 우선 로드
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
                "description": entry.get("description", "")  # 비상용 백업 본문 보관
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
        
        # 웹 본문 추출 + 피드 백업 연동
        body_text = fetch_article_content(
            page_url=item["link"],
            target_dept=item["dept"],
            fallback_description=item.get("description", "")
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
        time.sleep(0.35)  # 노션 API Rate limit 방지

    print("=== [완료] 모든 보도자료 본문 추출 및 노션 적재 성공! ===")

if __name__ == "__main__":
    main()
