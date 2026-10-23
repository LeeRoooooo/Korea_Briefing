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
# 1. 환경 변수 및 노션 클라이언트 설정
# -------------------------------------------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

# 재사용 HTTP 세션
session = requests.Session()

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
# 2. 모바일 URL 변환 및 더미 데이터 완벽 절단 정제기
# -------------------------------------------------------------
def convert_to_mobile_url(url):
    """PC URL을 m.korea.kr 모바일 주소로 강제 변환"""
    if not url:
        return ""
    mobile_url = url.replace("://www.korea.kr", "://m.korea.kr")
    if "pWise=mSub" not in mobile_url:
        separator = "&" if "?" in mobile_url else "?"
        mobile_url = f"{mobile_url}{separator}pWise=mSub"
    return mobile_url

def clean_html_to_text(element):
    """
    HTML 요소에서 잡음 태그를 삭제하고,
    '이전다음기사', '공유열기', '운영원칙' 등 하단 더미 데이터 직전까지만 절단
    """
    if not element:
        return ""

    # 불필요한 태그 및 공통 레이아웃 제거
    noise_selectors = [
        ".remark_briefing", ".law_copy_wrap", ".filedown", ".kogl", ".btn",
        ".prev_next", ".prev_next_wrap", ".sns_share", ".sns_wrap",
        ".reply_wrap", ".comment_wrap", ".view_btn_wrap",
        "script", "style", "noscript"
    ]
    for sel in noise_selectors:
        for tag in element.select(sel):
            tag.decompose()

    # 줄바꿈 태그 변환
    for br in element.find_all("br"):
        br.replace_with("\n")
    for p in element.find_all(["p", "div", "li"]):
        p.append("\n")

    raw_text = html.unescape(element.get_text())

    # 하단 더미 데이터 차단 트리거 키워드
    cutoff_keywords = [
        "이전다음기사", "다음기사", "이전기사", "공유열기", "공유 닫기",
        "댓글보기", "운영원칙 열기", "정책브리핑 게시물 운영원칙",
        "1. 타인의 메일주소, 전화번호", "저작권정책", "담당자안내"
    ]

    cleaned_lines = []
    for line in raw_text.splitlines():
        line_str = re.sub(r"[ \t]+", " ", line).strip()
        
        # 더미 데이터 시작 지점을 만나면 파싱 즉시 종료
        if any(keyword in line_str for keyword in cutoff_keywords):
            break

        if "보도자료를 전재하여 제공함을 알려드립니다" in line_str:
            continue

        if line_str:
            cleaned_lines.append(line_str)

    return "\n\n".join(cleaned_lines)

def fetch_mobile_article_content(pc_url, target_dept, fallback_desc=""):
    """
    m.korea.kr 모바일 주소로 본문 추출 및 더미 데이터 제거
    """
    mobile_url = convert_to_mobile_url(pc_url)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Referer": "https://m.korea.kr/briefing/pressReleaseList.do?pWise=mSub",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9"
    }

    try:
        res = session.get(mobile_url, timeout=7.0, headers=headers)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            view_containers = soup.find_all("div", class_="view_cont")
            target_container = None

            # 1순위: data-title 부처명 일치 확인
            clean_dept = target_dept.strip() if target_dept else ""
            if clean_dept and clean_dept != "담당부처 미상":
                for vc in view_containers:
                    data_title = vc.get("data-title", "").strip()
                    if clean_dept in data_title or data_title in clean_dept:
                        target_container = vc
                        break

            # 2순위: 일치 항목이 없으면 글자 수가 가장 긴 view_cont 컨테이너 선택
            if not target_container and view_containers:
                target_container = max(view_containers, key=lambda c: len(c.get_text(strip=True)))

            # 3순위: article_body 컨테이너 확인
            if not target_container:
                target_container = soup.find("div", class_="article_body")

            if target_container:
                text_content = clean_html_to_text(target_container)
                if len(text_content.strip()) >= 30:
                    return text_content
    except Exception:
        pass

    # 통신 실패 시 피드 요약본 안전망
    if fallback_desc and len(fallback_desc.strip()) > 10:
        desc_soup = BeautifulSoup(fallback_desc, "html.parser")
        desc_text = desc_soup.get_text(separator="\n\n").strip()
        if len(desc_text) >= 15:
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
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===", flush=True)

    feed_source = "feed.xml" if os.path.exists("feed.xml") else FEED_URL
    feed = feedparser.parse(feed_source)
    print(f"[*] 총 피드 항목 수: {len(feed.entries)}개", flush=True)

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

    print(f"[*] 매칭된 어제자 보도자료: {len(target_entries)}개", flush=True)

    if not target_entries:
        print("[!] 대상 날짜 기사가 없어 작업을 안전하게 종료합니다.", flush=True)
        return

    # 노션 상위 항목 생성
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
    print(f"[+] 노션 상위 항목 생성 완료: {parent_title}", flush=True)

    # 하위 보도자료 세부 페이지 등록
    for idx, item in enumerate(target_entries, start=1):
        child_title = f"[{idx:02d}] [{item['dept']}] {item['title']}"
        print(f"  -> [{idx}/{len(target_entries)}] 처리 중: [{item['dept']}] {item['title'][:20]}...", flush=True)
        
        body_text = fetch_mobile_article_content(
            pc_url=item["link"],
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
        for p_chunk in paragraphs[:25]:
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
        time.sleep(0.3)

    print("=== [완료] 불필요한 더미 데이터 없이 본문만 완벽 적재되었습니다! ===", flush=True)

if __name__ == "__main__":
    main()
