import os
import re
import io
import time
from datetime import datetime, timedelta
import zoneinfo
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from pypdf import PdfReader
from notion_client import Client

# 환경 변수
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")
FEED_URL = "https://leeroooooo.github.io/Korea_Briefing/feed.xml"

notion = Client(auth=NOTION_TOKEN)
KST = zoneinfo.ZoneInfo("Asia/Seoul")

# 불필요한 정부 웹사이트 공통 문구 필터링 키워드
FILTER_WORDS = [
    "공식 전자정부 누리집", "공유열기", "페이스북", "카카오톡", "저작권",
    "공공누리", "본문듣기", "글자크기", "프린트", "게시물 운영원칙",
    "바로보기", "내려받기", "이전기사", "다음기사", "목록"
]

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

def extract_department_and_clean_title(raw_title):
    """
    제목의 '[질병관리청]' 등에서 부처명을 정확히 추출하고,
    정제된 순수 제목을 반환
    """
    match = re.search(r"^\[(.*?)\]", raw_title.strip())
    if match:
        dept = match.group(1).strip()
        # 제목 앞의 [부처명] 제거
        clean_title = re.sub(r"^\[.*?\]\s*", "", raw_title).strip()
        return dept, clean_title
    return "담당부처 미상", raw_title.strip()

def extract_pdf_text_from_url(pdf_url):
    """PDF 파일을 메모리 상에서 다운로드하여 텍스트만 추출"""
    try:
        res = requests.get(pdf_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200:
            pdf_file = io.BytesIO(res.content)
            reader = PdfReader(pdf_file)
            full_text = []
            for page in reader.pages[:10]: # 최대 10페이지까지 추출
                text = page.extract_text()
                if text:
                    full_text.append(text.strip())
            return "\n\n".join(full_text)
    except Exception as e:
        print(f"      [!] PDF 텍스트 추출 실패: {e}")
    return None

def fetch_real_content(page_url):
    """
    상세 페이지 접속 -> 첨부된 PDF 링크 우선 탐색 후 텍스트 추출
    PDF가 없는 경우 정제된 웹 본문 텍스트 추출
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(page_url, timeout=15, headers=headers)
        if res.status_code != 200:
            return "상세 페이지에 접속할 수 없습니다."
        
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 1. 페이지 내 PDF 첨부파일 링크 탐색
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if ".pdf" in href.lower():
                # 상대 경로인 경우 절대 경로로 결합
                if href.startswith("/"):
                    pdf_url = f"https://www.korea.kr{href}"
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = f"https://www.korea.kr/{href}"
                
                print(f"      [*] PDF 파일 발견 및 텍스트 추출 시도: {pdf_url}")
                pdf_text = extract_pdf_text_from_url(pdf_url)
                if pdf_text and len(pdf_text) > 100:
                    return pdf_text

        # 2. PDF가 없거나 텍스트가 안 뽑힐 경우 웹 본문 정밀 크롤링
        # 불필요한 태그 제거
        for tag in soup(["script", "style", "nav", "footer", "header", "form", "aside"]):
            tag.decompose()

        # 본문 후보 영역 탐색 (보통 .article-content, #articleText 등)
        body_candidates = soup.select(".article-content, .view-con, .board-view, #articleText")
        target_soup = body_candidates[0] if body_candidates else soup

        valid_paragraphs = []
        for p in target_soup.find_all(["p", "div"]):
            txt = p.get_text(strip=True)
            # 공통 문구 및 짧은 메뉴 제외
            if len(txt) > 25 and not any(f in txt for f in FILTER_WORDS):
                valid_paragraphs.append(txt)

        unique_paragraphs = list(dict.fromkeys(valid_paragraphs))
        web_text = "\n\n".join(unique_paragraphs)
        return web_text if len(web_text) > 50 else "상세 본문 텍스트를 추출할 수 없습니다. 첨부파일 원본을 확인하세요."
        
    except Exception as e:
        return f"원문 추출 중 오류 발생: {e}"

def get_database_title_property_name(db_id):
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
    
    print(f"=== [수집 시작] 기준 대상일: {yesterday_fmt} ({day_str}요일) ===")

    feed_source = "feed.xml" if os.path.exists("feed.xml") else FEED_URL
    feed = feedparser.parse(feed_source)
    print(f"[*] 총 피드 항목 수: {len(feed.entries)}개")

    target_entries = []
    for entry in feed.entries:
        if extract_entry_date(entry) == yesterday:
            raw_title = entry.get("title", "제목 없음")
            dept, clean_title = extract_department_and_clean_title(raw_title)
            
            target_entries.append({
                "dept": dept,
                "title": clean_title,
                "link": entry.get("link", "")
            })

    print(f"[*] 필터링된 어제자 보도자료: {len(target_entries)}개")

    if not target_entries:
        print("[!] 대상 날짜 기사가 없어 종료합니다.")
        return

    clean_db_id = DATABASE_ID.replace("-", "").strip()
    title_prop_name = get_database_title_property_name(clean_db_id)
    parent_title = f"보도자료_{yesterday_fmt}.({day_str})"

    # 상위 데이터베이스 행 생성
    parent_page = notion.pages.create(
        parent={"database_id": clean_db_id},
        properties={
            title_prop_name: {"title": [{"text": {"content": parent_title}}]}
        }
    )
    parent_page_id = parent_page["id"]
    print(f"[+] 노션 상위 항목 생성: {parent_title}")

    # 하위 보도자료 페이지 생성
    for idx, item in enumerate(target_entries, start=1):
        child_title = f"[{idx:02d}] [{item['dept']}] {item['title']}"
        print(f"  -> [{idx}/{len(target_entries)}] '{item['dept']}' 본문 수집 중: {item['title'][:25]}...")
        
        detail_content = fetch_real_content(item["link"])

        # 노션 블록 2000자 제한 대응 (문단 쪼개기)
        content_blocks = [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [
                        {"text": {"content": f"담당부처: {item['dept']} | "}},
                        {"text": {"content": "정책브리핑 원문 바로가기", "link": {"url": item['link']}}}
                    ],
                    "icon": {"emoji": "🏛️"}
                }
            },
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": "보도자료 원문 내용"}}]}
            }
        ]

        # 본문 문단을 1500자 단위로 나누어 노션 블록으로 생성
        paragraphs = [p.strip() for p in detail_content.split("\n\n") if p.strip()]
        for p in paragraphs[:15]: # 최대 15개 문단 등록
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

    print("=== [완료] 원문 텍스트 추출 및 노션 적재 완료! ===")

if __name__ == "__main__":
    main()
