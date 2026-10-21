import asyncio
from datetime import datetime, timezone, timedelta
import email.utils
import re
from urllib.parse import urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET
from xml.dom import minidom

from playwright.async_api import async_playwright

BASE_URL = "https://www.korea.kr"
LIST_URL_TEMPLATE = "https://www.korea.kr/briefing/pressReleaseList.do?pageIndex={page_index}"
OUTPUT_XML_FILE = "feed.xml"

# 한국 표준시(KST) 정의
KST = timezone(timedelta(hours=9))

def clean_view_url(base_url: str, raw_href: str) -> str:
    """불필요한 쿼리스트링을 제거하고 newsId 식별자만 포함된 깔끔한 링크를 반환합니다."""
    if not raw_href:
        return base_url
    parsed = urlparse(raw_href)
    params = parse_qs(parsed.query)
    news_id = params.get("newsId", [""])[0]
    if news_id:
        return urljoin(base_url, f"{parsed.path}?newsId={news_id}")
    return urljoin(base_url, raw_href)

async def collect_press_releases(max_pages: int = 5):
    """지정된 최대 페이지 수만큼 순회하며 최신 보도자료를 수집합니다."""
    items = []
    
    async with async_playwright() as p:
        # GitHub Actions 환경에 최적화된 headless 브라우저 실행
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        page = await browser.new_page()

        for page_index in range(1, max_pages + 1):
            target_url = LIST_URL_TEMPLATE.format(page_index=page_index)
            print(f"[수집 중] 페이지 {page_index}: {target_url}")
            
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                print(f"페이지 로딩 오류({page_index}p): {e}")
                break

            article_links = await page.query_selector_all("a[href*='pressReleaseView.do']")
            if not article_links:
                break

            for a_el in article_links:
                source_spans = await a_el.query_selector_all("span.source span")
                if len(source_spans) < 2:
                    continue

                raw_date_text = (await source_spans[0].inner_text()).strip()
                department = (await source_spans[1].inner_text()).strip()

                # 날짜 파싱 (YYYY.MM.DD)
                try:
                    date_match = re.findall(r"\d{4}\.\d{2}\.\d{2}", raw_date_text)[0]
                    post_date = datetime.strptime(date_match, "%Y.%m.%d").date()
                except (IndexError, ValueError):
                    continue

                strong_el = await a_el.query_selector("strong")
                title_text = (await strong_el.inner_text()).strip() if strong_el else "보도자료"
                final_title = f"[{department}] {title_text}"

                lead_el = await a_el.query_selector("span.lead")
                summary = (await lead_el.inner_text()).strip() if lead_el else title_text

                raw_href = await a_el.get_attribute("href")
                final_link = clean_view_url(BASE_URL, raw_href)

                post_datetime = datetime.combine(post_date, datetime.min.time(), tzinfo=KST)

                items.append({
                    "title": final_title,
                    "link": final_link,
                    "description": summary,
                    "pub_date": post_datetime,
                    "guid": final_link
                })

        await browser.close()
        
    return items

def build_rss_file(items: list, output_filepath: str):
    """수집된 목록을 최신순으로 정렬하여 유효한 RSS 2.0 XML 문서를 생성합니다."""
    # 중복 제거 (guid 기준)
    unique_items = {}
    for item in items:
        if item["guid"] not in unique_items:
            unique_items[item["guid"]] = item

    # 최신일자 내림차순 정렬
    sorted_items = sorted(unique_items.values(), key=lambda x: x["pub_date"], reverse=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "대한민국 정책브리핑 보도자료"
    ET.SubElement(channel, "link").text = "https://www.korea.kr/briefing/pressReleaseList.do"
    ET.SubElement(channel, "description").text = "대한민국 정책브리핑(korea.kr)의 최신 보도자료 공식 피드"
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
        
    print(f"총 {len(sorted_items)}건의 피드가 '{output_filepath}'로 생성되었습니다.")

async def main():
    items = await collect_press_releases(max_pages=3)
    build_rss_file(items, OUTPUT_XML_FILE)

if __name__ == "__main__":
    asyncio.run(main())
