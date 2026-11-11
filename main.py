import asyncio
from datetime import datetime, timezone, timedelta
import email.utils
import os
import re
from urllib.parse import urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET
from xml.dom import minidom

from playwright.async_api import async_playwright

# ==========================================
# 환경 설정 및 상수 정의
# ==========================================
BASE_URL = "https://www.korea.kr"
LIST_URL_TEMPLATE = "https://www.korea.kr/briefing/pressReleaseList.do?pageIndex={page_index}"
OUTPUT_XML_FILE = "feed.xml"

# [수정] 화요일 등 대량 발행일(60~90건) 누락 방지를 위해 기본 탐색 범위를 7페이지로 확대
DEFAULT_MAX_PAGES = 7

# 한국 표준시(KST: UTC+9) 고정 (GitHub Actions 러너 환경의 시차 왜곡 방지)
KST = timezone(timedelta(hours=9))


def clean_view_url(base_url: str, raw_href: str) -> str:
    """불필요한 검색 파라미터를 정리하고 고유 newsId만 남긴 깔끔한 링크를 반환합니다."""
    if not raw_href:
        return base_url
    parsed = urlparse(raw_href)
    params = parse_qs(parsed.query)
    news_id = params.get("newsId", [""])[0]
    if news_id:
        return urljoin(base_url, f"{parsed.path}?newsId={news_id}")
    return urljoin(base_url, raw_href)


def load_existing_feed(filepath: str) -> dict:
    """기존 feed.xml이 있다면 읽어와 {link: data} 딕셔너리로 반환합니다."""
    existing_items = {}
    if not os.path.exists(filepath):
        print(f"[*] 기존 '{filepath}' 파일이 없어 새로 생성을 준비합니다.")
        return existing_items

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is None:
            return existing_items

        for item_el in channel.findall("item"):
            link_el = item_el.find("link")
            title_el = item_el.find("title")
            desc_el = item_el.find("description")
            pub_date_el = item_el.find("pubDate")
            guid_el = item_el.find("guid")

            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not link:
                continue

            pub_date = None
            if pub_date_el is not None and pub_date_el.text:
                try:
                    pub_date = email.utils.parsedate_to_datetime(pub_date_el.text)
                except Exception:
                    pub_date = datetime.now(KST)
            else:
                pub_date = datetime.now(KST)

            existing_items[link] = {
                "title": title_el.text if title_el is not None and title_el.text else "",
                "link": link,
                "description": desc_el.text if desc_el is not None and desc_el.text else "",
                "guid": guid_el.text if guid_el is not None and guid_el.text else link,
                "pub_date": pub_date
            }
        print(f"[*] 기존 피드에서 {len(existing_items)}개의 게시글을 성공적으로 불러왔습니다.")
    except Exception as e:
        print(f"[!] 기존 피드 읽기 예외 발생: {e}")

    return existing_items


async def collect_press_releases(max_pages: int = DEFAULT_MAX_PAGES):
    """
    지정된 최대 페이지 수(기본 7페이지)를 순회하며 최신 보도자료를 수집합니다.
    """
    items = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        page = await browser.new_page()

        for page_index in range(1, max_pages + 1):
            target_url = LIST_URL_TEMPLATE.format(page_index=page_index)
            print(f"[수집 시도] {page_index}/{max_pages} 페이지 로딩 중: {target_url}")
            
            try:
                # 네트워크 유휴 상태까지 최대 30초 대기
                await page.goto(target_url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                print(f"[!] 페이지 로드 실패({page_index}p): {e}")
                break

            article_links = await page.query_selector_all("a[href*='pressReleaseView.do']")
            if not article_links:
                print(f"[*] {page_index}페이지에서 더 이상 게시물을 찾을 수 없습니다.")
                break

            for a_el in article_links:
                # 1. 날짜 및 부처명 추출
                source_spans = await a_el.query_selector_all("span.source span")
                if len(source_spans) < 2:
                    continue

                raw_date_text = (await source_spans[0].inner_text()).strip()
                department = (await source_spans[1].inner_text()).strip()

                try:
                    date_match = re.findall(r"\d{4}\.\d{2}\.\d{2}", raw_date_text)[0]
                    post_date = datetime.strptime(date_match, "%Y.%m.%d").date()
                except (IndexError, ValueError):
                    continue

                # 2. 제목 추출
                strong_el = await a_el.query_selector("strong")
                title_text = (await strong_el.inner_text()).strip() if strong_el else "보도자료"
                final_title = f"[{department}] {title_text}"

                # 3. 요약문 추출
                lead_el = await a_el.query_selector("span.lead")
                summary = (await lead_el.inner_text()).strip() if lead_el else title_text

                # 4. 링크 정리 (newsId 기준 Canonical URL 생성)
                raw_href = await a_el.get_attribute("href")
                final_link = clean_view_url(BASE_URL, raw_href)

                # 타임존(KST) 부여
                post_datetime = datetime.combine(post_date, datetime.min.time(), tzinfo=KST)

                items.append({
                    "title": final_title,
                    "link": final_link,
                    "description": summary,
                    "pub_date": post_datetime,
                    "guid": final_link
                })

            # 서버 부하 방지 및 레이트 리밋 예방 미세 딜레이
            await asyncio.sleep(0.4)

        await browser.close()
        
    return items


def save_rss_xml(items: list, output_filepath: str):
    """최신 날짜 내림차순으로 정렬하여 표준 RSS 2.0 XML 문서를 저장합니다."""
    sorted_items = sorted(items, key=lambda x: x["pub_date"], reverse=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "대한민국 정책브리핑 보도자료"
    ET.SubElement(channel, "link").text = "https://www.korea.kr/briefing/pressReleaseList.do"
    ET.SubElement(channel, "description").text = "대한민국 정책브리핑(korea.kr) 누적 보도자료 피드"
    ET.SubElement(channel, "language").text = "ko-KR"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(datetime.now(KST))

    for item in sorted_items:
        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = item["title"]
        ET.SubElement(item_el, "link").text = item["link"]
        ET.SubElement(item_el, "description").text = item["description"]
        ET.SubElement(item_el, "guid").text = item["guid"]
        ET.SubElement(item_el, "pubDate").text = email.utils.format_datetime(item["pub_date"])

    xml_bytes = ET.tostring(rss, encoding="utf-8")
    pretty_xml = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")

    with open(output_filepath, "wb") as f:
        f.write(pretty_xml)
        
    print(f"[*] 총 {len(sorted_items)}건의 보도자료가 최신순으로 정렬되어 '{output_filepath}'에 반영되었습니다.")


async def main():
    # 1. 기존 XML 파일 읽기
    existing_items_dict = load_existing_feed(OUTPUT_XML_FILE)

    # 2. 웹에서 최신 7페이지 스크래핑 (DEFAULT_MAX_PAGES 상수 적용)
    scraped_items = await collect_press_releases(max_pages=DEFAULT_MAX_PAGES)
    print(f"[*] 스크래핑 완료: {len(scraped_items)}건 수집")

    # 3. 고유 URL(link) 기준으로 병합 (중복 자동 제거)
    new_added_count = 0
    for item in scraped_items:
        if item["link"] not in existing_items_dict:
            new_added_count += 1
        existing_items_dict[item["link"]] = item

    print(f"[*] 신규 누적 게시물: {new_added_count}건 (전체 보관: {len(existing_items_dict)}건)")

    # 4. 전체 누적 항목을 최신순으로 정렬하여 XML 재저장
    save_rss_xml(list(existing_items_dict.values()), OUTPUT_XML_FILE)


if __name__ == "__main__":
    asyncio.run(main())
