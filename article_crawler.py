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

依赖：requests、beautifulsoup4、reportlab，中文字体用 Noto Sans SC，
工作流里会先下载字体文件（NotoSansSC.ttf）再跑这个脚本。
本地跑之前也要先准备好字体文件，见 --font 参数。

用法：
    python article_crawler.py                        # 抓当天（自动回退）
    python article_crawler.py --date 2026-11-05     # 抓指定日期
    python article_crawler.py --font ./NotoSansSC.ttf
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
        # 跳过“本版责编”这种不是文章的条目
        if not title or title.startswith("本版责编"):
            continue
        articles.append((title, urljoin(section_url, a["href"]), section_name))
    return articles


def get_article_text(url):
    """下载一篇文章的正文：返回纯文本段落列表，失败返回空列表。"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.encoding = "utf-8"
    except Exception as e:
        logging.warning(f"文章 {url} 下载失败：{e}")
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    content = soup.find("div", class_="article-content") or soup.find("div", id="articleContent")
    if not content:
        logging.warning(f"文章 {url} 里没有找到正文")
        return []
    paragraphs = []
    for p in content.find_all("p"):
        text = p.get_text(strip=True)
        if text:
            paragraphs.append(text)
    return paragraphs


def fetch_all_articles(date):
    """抓全部版面、全部文章正文，按版面顺序返回。

    返回 [(版面名, 标题, [段落, ...]), ...]，去重（同一篇文章只保留一次）。
    多线程下载，但用 executor.map 保持顺序，不打乱版面和文章顺序。
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
        return section_name, title, get_article_text(url)

    articles = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for i, (section_name, title, paragraphs) in enumerate(
            executor.map(_fetch, unique_links), 1
        ):
            if not paragraphs:
                logging.warning(f"[{i}/{len(unique_links)}] 正文为空，已跳过：{title}")
                continue
            articles.append((section_name, title, paragraphs))
            logging.info(f"[{i}/{len(unique_links)}] 完成：{title}（{len(paragraphs)} 段）")
    logging.info(f"正文下载完成，共 {len(articles)} 篇有效文章")
    return articles


def build_pdf(date, articles, font_path, out_path):
    """把文章列表生成 PDF：封面标题、按版面分组的目录、正文、页脚页码。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    if not os.path.isfile(font_path):
        logging.error(f"找不到中文字体文件：{font_path}，请先下载 Noto Sans SC 再运行")
        return False
    pdfmetrics.registerFont(TTFont("NotoSansSC", font_path))

    red = HexColor("#c8102e")
    grey = HexColor("#666666")
    date_str = date.strftime("%Y-%m-%d")
    title_style = ParagraphStyle("标题", fontName="NotoSansSC", fontSize=15, leading=22,
                                 alignment=TA_CENTER, spaceAfter=6, textColor=red)
    section_style = ParagraphStyle("版面", fontName="NotoSansSC", fontSize=13, leading=20,
                                   spaceBefore=14, spaceAfter=6, textColor=red)
    body_style = ParagraphStyle("正文", fontName="NotoSansSC", fontSize=11.5, leading=18,
                                alignment=TA_JUSTIFY, firstLineIndent=23, spaceAfter=6)
    toc_title_style = ParagraphStyle("目录标题", fontName="NotoSansSC", fontSize=16, leading=24,
                                     alignment=TA_CENTER, spaceAfter=12)
    toc_section_style = ParagraphStyle("目录版面", fontName="NotoSansSC", fontSize=12, leading=20,
                                       spaceBefore=10, spaceAfter=4)
    toc_item_style = ParagraphStyle("目录条目", fontName="NotoSansSC", fontSize=11, leading=17,
                                    leftIndent=12)
    cover_title_style = ParagraphStyle("封面标题", fontName="NotoSansSC", fontSize=24, leading=34,
                                       alignment=TA_CENTER, textColor=red)
    cover_sub_style = ParagraphStyle("封面副标题", fontName="NotoSansSC", fontSize=13, leading=22,
                                     alignment=TA_CENTER, textColor=grey)

    def footer(canvas, doc):
        """每页页脚：第 x 页 · 人民日报文章汇总 日期。"""
        canvas.saveState()
        canvas.setFont("NotoSansSC", 9)
        canvas.setFillColor(grey)
        canvas.drawCentredString(A4[0] / 2, 14 * mm,
                                 f"第 {doc.page} 页 · 人民日报文章汇总 {date_str}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=f"人民日报文章汇总 {date_str}",
        author="人民日报文章汇总",
    )
    story = []
    # 封面
    story.append(Spacer(1, 60 * mm))
    story.append(Paragraph("人民日报文章汇总", cover_title_style))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(f"{date.strftime('%Y年%m月%d日')} · 共 {len(articles)} 篇文章",
                           cover_sub_style))
    story.append(Paragraph("仅限自己学习使用，不得用于商业活动。", cover_sub_style))
    story.append(PageBreak())

    # 目录：按版面分组，只列标题，不标页码
    story.append(Paragraph("目录", toc_title_style))
    current_section = None
    for section_name, title, _ in articles:
        if section_name != current_section:
            current_section = section_name
            story.append(Paragraph(html.escape(section_name), toc_section_style))
        story.append(Paragraph(html.escape(title), toc_item_style))
    story.append(PageBreak())

    # 正文：按版面分组，每篇文章标题居中，正文段首空两格
    current_section = None
    for i, (section_name, title, paragraphs) in enumerate(articles):
        if section_name != current_section:
            current_section = section_name
            story.append(Paragraph(html.escape(section_name), section_style))
        story.append(Paragraph(html.escape(title), title_style))
        for text in paragraphs:
            story.append(Paragraph(html.escape(text), body_style))
        if i < len(articles) - 1:
            story.append(Spacer(1, 4 * mm))
            story.append(HRFlowable(width="100%", thickness=0.5, color=grey))
            story.append(Spacer(1, 4 * mm))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    logging.info(f"文章汇总版 PDF 已生成：{out_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="抓取人民日报当日文章，生成文章汇总版 PDF")
    parser.add_argument("--date", default=None,
                        help="指定日期，格式 YYYY-MM-DD；不指定则自动识别当天（报纸没出就回退到昨天）")
    parser.add_argument("--font", default="NotoSansSC.ttf",
                        help="中文字体文件路径，默认当前目录下的 NotoSansSC.ttf")
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
    if not articles:
        logging.error("没有抓到任何文章正文，不生成 PDF")
        return 1

    date_str = date.strftime("%Y-%m-%d")
    out_path = args.out or os.path.join(OUTPUT_DIR, f"人民日报-{date_str}-文章汇总版.pdf")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not build_pdf(date, articles, args.font, out_path):
        return 1
    logging.info(f"文章汇总版完成：{date_str}，共 {len(articles)} 篇文章")
    return 0


if __name__ == "__main__":
    sys.exit(main())
