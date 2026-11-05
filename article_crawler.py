#!/usr/bin/env python3
"""抓取人民日报当日全部版面文章正文，合并生成一份文章汇总版 PDF。

来源：原仓库 rmrb-pdf-fetcher 的 article-crawler 分支。
那个分支里的旧脚本有两个问题，这里都修了：
1. 旧脚本把日期写死成 2025-10-19，只能抓那一天；这里改成自动识别当天日期，
   当天报纸还没出就自动回退到前一天。
2. 旧脚本只抓头版（node_01）一个版面，而且只保存成 txt；
   这里抓全部版面（node_01 到 node_20 等），多线程下载正文，
   最后用 reportlab 生成带目录和页码的 PDF。

输出：人民日报下载/人民日报-<日期>-文章汇总版.pdf
网页上的文章汇总版就是这个文件，文件名约定见 generate_index.py：
pdfs/<日期>-article.pdf -> 文章汇总版。

依赖：requests、beautifulsoup4、reportlab，中文字体用 Noto Serif SC（宋体风格，
和之前文章汇总版用的 SimSun 效果接近），工作流里会先下载字体文件
（NotoSerifSC.ttf）再跑这个脚本。本地跑之前也要先准备好字体文件，见 --font 参数。

版面效果和之前的文章汇总版保持一致（以 2025-10-20 那份 228 页的汇总版为准）：
- 没有封面，第一页就是目录，标题“人民日报文章目录”，条目按网站顺序连续编号，
  每条都能点、点进去跳到对应文章。
- 正文每篇文章另起一页：标题大字居中，下面是作者（有的话）和《人民日报》引用行，
  再画一条横线，然后是正文；正文宋体 12 号，段首空两格，右边留约 6 厘米空白当笔记区。
- 每页底下只写“第 x 页”。

用法：
    python article_crawler.py                        # 抓当天（自动回退）
    python article_crawler.py --date 2026-11-05     # 抓指定日期
    python article_crawler.py --font ./NotoSerifSC.ttf
"""
import argparse
import html
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("article_crawler.log"),
        logging.StreamHandler(),
    ],
)

# 人民日报电子版电脑版布局页地址模板，{date} 形如 202511/05
LAYOUT_URL = "https://paper.people.com.cn/rmrb/pc/layout/{date}/node_01.html"
# 版面链接只认 node_01.html 这种格式，手机版（pad/layout 下的链接）不要
SECTION_RE = re.compile(r"^node_\d+\.html$")

# 请求头，模拟浏览器访问
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://paper.people.com.cn/rmrb/",
}

# 输出目录和文件名：和完整版爬虫保持同一套目录
OUTPUT_DIR = "人民日报下载"


def beijing_today():
    """返回北京时间今天的日期对象。"""
    beijing = timezone(timedelta(hours=8))
    return datetime.now(beijing).date()


def detect_date():
    """确定要抓的日期：先试今天，今天的报纸还没出（页面打不开）就回退到昨天。"""
    today = beijing_today()
    for day in (today, today - timedelta(days=1)):
        url = LAYOUT_URL.format(date=day.strftime("%Y%m/%d"))
        try:
            res = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            logging.warning(f"访问 {url} 失败：{e}，换一天试试")
            continue
        if res.status_code == 200 and "news-list" in res.text:
            logging.info(f"确定抓取日期：{day}（{url}）")
            return day
        logging.warning(f"{day} 的报纸页面打不开（状态码 {res.status_code}），换一天试试")
    logging.error("今天和昨天的报纸页面都打不开，无法确定日期")
    return None


def get_sections(date):
    """获取当天全部版面：返回 [(版面名, 版面地址), ...]，按版面号排序。"""
    base = LAYOUT_URL.format(date=date.strftime("%Y%m/%d"))
    res = requests.get(base, headers=HEADERS, timeout=20)
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    sections = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if SECTION_RE.match(href):
            sections[href] = a.get_text(strip=True) or href
    result = [(name, urljoin(base, href)) for href, name in sorted(sections.items())]
    logging.info(f"共发现 {len(result)} 个版面")
    return result


def get_articles_from_section(section_name, section_url):
    """获取一个版面里的文章链接：返回 [(标题, 文章地址, 版面名), ...]。"""
    try:
        res = requests.get(section_url, headers=HEADERS, timeout=20)
        res.encoding = "utf-8"
    except Exception as e:
        logging.warning(f"版面 {section_name} 访问失败：{e}")
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    news_list = soup.find("ul", class_="news-list")
    if not news_list:
        logging.warning(f"版面 {section_name} 里没有找到文章列表")
        return []
    articles = []
    for a in news_list.find_all("a", href=True):
        title = a.get_text(strip=True)
        # 目录里原样保留网站列出的全部条目（包括“本版责编”“广告”这些），
        # 和之前的文章汇总版一致：目录按网站顺序连续编号。
        if title:
            articles.append((title, urljoin(section_url, a["href"]), section_name))
    return articles


def get_article(url):
    """下载一篇文章：返回 (作者, 引用行, 正文段落列表)，失败返回 ("", "", [])。

    文章页里的 p.sec 一行里同时写着作者和引用，例如：
    “新华社记者《人民日报》（2025年10月20日 第 01 版）”，
    《 前面是作者，从 《 开始是引用行；没有作者的就只有引用行。
    """
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.encoding = "utf-8"
    except Exception as e:
        logging.warning(f"文章 {url} 下载失败：{e}")
        return "", "", []
    soup = BeautifulSoup(res.text, "html.parser")
    author, citation = "", ""
    sec = soup.find("p", class_="sec")
    if sec:
        sec_text = sec.get_text(strip=True)
        pos = sec_text.find("《")
        if pos > 0:
            author, citation = sec_text[:pos].strip(), sec_text[pos:].strip()
        else:
            citation = sec_text
    content = soup.find("div", class_="article-content") or soup.find("div", id="articleContent")
    if not content:
        logging.warning(f"文章 {url} 里没有找到正文")
        return author, citation, []
    paragraphs = []
    for p in content.find_all("p"):
        text = p.get_text(strip=True)
        if text:
            paragraphs.append(text)
    return author, citation, paragraphs


def fetch_all_articles(date):
    """抓全部版面、全部文章正文，按版面顺序返回。

    返回 [(版面名, 标题, 作者, 引用行, [段落, ...]), ...]，去重（同一篇文章只保留一次）。
    多线程下载，但用 executor.map 保持顺序，不打乱版面和文章顺序。
    目录里保留全部条目：正文为空的（比如纯图片页）只进目录、不进正文，
    和之前的文章汇总版一致。
    """
    sections = get_sections(date)
    if not sections:
        return []
    all_links = []
    for section_name, section_url in sections:
        all_links.extend(get_articles_from_section(section_name, section_url))
    # 按文章地址去重
    seen = set()
    unique_links = []
    for title, url, section_name in all_links:
        if url not in seen:
            seen.add(url)
            unique_links.append((title, url, section_name))
    logging.info(f"共发现 {len(unique_links)} 篇文章，开始下载正文")

    def _fetch(item):
        title, url, section_name = item
        author, citation, paragraphs = get_article(url)
        return section_name, title, author, citation, paragraphs

    articles = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for i, (section_name, title, author, citation, paragraphs) in enumerate(
            executor.map(_fetch, unique_links), 1
        ):
            if not paragraphs:
                logging.warning(f"[{i}/{len(unique_links)}] 正文为空，只进目录、不进正文：{title}")
            else:
                logging.info(f"[{i}/{len(unique_links)}] 完成：{title}（{len(paragraphs)} 段）")
            articles.append((section_name, title, author, citation, paragraphs))
    with_body = sum(1 for a in articles if a[4])
    logging.info(f"正文下载完成，目录共 {len(articles)} 条，其中 {with_body} 篇有正文")
    return articles


def build_pdf(date, articles, font_path, out_path):
    """把文章列表生成 PDF，版面和之前的文章汇总版一致：

    - 没有封面，第一页就是目录：标题“人民日报文章目录”，条目按网站顺序连续编号，
      有正文的条目可以点、点进去跳到对应文章。
    - 正文每篇文章另起一页：标题大字居中，作者和引用行居中，横线下面是正文；
      正文宋体 12 号、段首空两格，右边留约 6 厘米空白当笔记区。
    - 每页底下只写“第 x 页”。
    """
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        HRFlowable,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
    )

    if not os.path.isfile(font_path):
        logging.error(f"找不到中文字体文件：{font_path}，请先下载 Noto Serif SC 再运行")
        return False
    pdfmetrics.registerFont(TTFont("NotoSerifSC", font_path))

    black = HexColor("#000000")
    date_str = date.strftime("%Y-%m-%d")
    # 目录页：通栏；正文页：窄栏，右边留约 6 厘米空白当笔记区
    toc_frame = Frame(25 * mm, 15 * mm, A4[0] - 50 * mm, A4[1] - 30 * mm, id="toc")
    body_frame = Frame(16.5 * mm, 19 * mm, 136 * mm, A4[1] - 38 * mm, id="body")

    def footer(canvas, doc):
        """每页页脚：只写“第 x 页”，居中。"""
        canvas.saveState()
        canvas.setFont("NotoSerifSC", 9)
        canvas.setFillColor(black)
        canvas.drawCentredString(A4[0] / 2, 11 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    doc = BaseDocTemplate(
        out_path, pagesize=A4,
        title=f"人民日报文章汇总 {date_str}",
        author="人民日报文章汇总",
    )
    doc.addPageTemplates([
        PageTemplate(id="toc", frames=[toc_frame], onPage=footer),
        PageTemplate(id="body", frames=[body_frame], onPage=footer),
    ])

    toc_title_style = ParagraphStyle("目录标题", fontName="NotoSerifSC", fontSize=20, leading=28,
                                     alignment=TA_CENTER, spaceAfter=18, wordWrap="CJK")
    toc_item_style = ParagraphStyle("目录条目", fontName="NotoSerifSC", fontSize=12, leading=22,
                                    wordWrap="CJK")
    title_style = ParagraphStyle("标题", fontName="NotoSerifSC", fontSize=26, leading=36,
                                 alignment=TA_CENTER, spaceAfter=10, wordWrap="CJK")
    author_style = ParagraphStyle("作者", fontName="NotoSerifSC", fontSize=10, leading=14,
                                  alignment=TA_CENTER, spaceAfter=4, wordWrap="CJK")
    citation_style = ParagraphStyle("引用行", fontName="NotoSerifSC", fontSize=10, leading=14,
                                    alignment=TA_CENTER, spaceAfter=8, wordWrap="CJK")
    body_style = ParagraphStyle("正文", fontName="NotoSerifSC", fontSize=12, leading=21,
                                alignment=TA_JUSTIFY, firstLineIndent=24, spaceAfter=8,
                                wordWrap="CJK")
    # 正文里没有标点、字又少的短行是小标题，居中排（比如“领航定向 驾驭发展全局创造新的历史伟业”）
    subhead_style = ParagraphStyle("小标题", fontName="NotoSerifSC", fontSize=12, leading=21,
                                   alignment=TA_CENTER, spaceBefore=6, spaceAfter=6,
                                   wordWrap="CJK")

    story = []
    # 目录：按网站顺序连续编号，只列标题，不标页码；有正文的条目做成可点链接
    story.append(Paragraph("人民日报文章目录", toc_title_style))
    for i, (_, title, _, _, paragraphs) in enumerate(articles, 1):
        text = f"{i}. {html.escape(title)}"
        if paragraphs:
            text = f'<link href="#art{i}">{text}</link>'
        story.append(Paragraph(text, toc_item_style))

    # 正文：每篇文章另起一页
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    for i, (_, title, author, citation, paragraphs) in enumerate(articles, 1):
        if not paragraphs:
            continue
        if i > 1:
            story.append(PageBreak())
        # 标题里放一个锚点，目录点进来就跳到这里
        story.append(Paragraph(f'<a name="art{i}"/>{html.escape(title)}', title_style))
        if author:
            story.append(Paragraph(html.escape(author), author_style))
        if citation:
            story.append(Paragraph(html.escape(citation), citation_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=black, spaceAfter=10))
        for text in paragraphs:
            plain = text.replace(" ", "").replace("　", "")
            if len(plain) <= 22 and not any(c in text for c in "。！？；，：、…"):
                story.append(Paragraph(html.escape(text), subhead_style))
            else:
                story.append(Paragraph(html.escape(text), body_style))

    doc.build(story)
    logging.info(f"文章汇总版 PDF 已生成：{out_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="抓取人民日报当日文章，生成文章汇总版 PDF")
    parser.add_argument("--date", default=None,
                        help="指定日期，格式 YYYY-MM-DD；不指定则自动识别当天（报纸没出就回退到昨天）")
    parser.add_argument("--font", default="NotoSerifSC.ttf",
                        help="中文字体文件路径，默认当前目录下的 NotoSerifSC.ttf")
    parser.add_argument("--out", default=None, help="输出 PDF 路径，不指定则按默认规则存放")
    parser.add_argument("--workers", type=int, default=10, help="同时下载文章的线程数，默认 10")
    args = parser.parse_args()

    if args.date:
        try:
            date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logging.error(f"日期格式不对：{args.date}，请用 YYYY-MM-DD")
            return 1
    else:
        date = detect_date()
        if date is None:
            return 1

    articles = fetch_all_articles(date)
    if not any(a[4] for a in articles):
        logging.error("没有抓到任何文章正文，不生成 PDF")
        return 1

    date_str = date.strftime("%Y-%m-%d")
    out_path = args.out or os.path.join(OUTPUT_DIR, f"人民日报-{date_str}-文章汇总版.pdf")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not build_pdf(date, articles, args.font, out_path):
        return 1
    with_body = sum(1 for a in articles if a[4])
    logging.info(f"文章汇总版完成：{date_str}，目录 {len(articles)} 条、正文 {with_body} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
