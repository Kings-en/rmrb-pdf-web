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
- 正文每篇文章另起一页：标题、作者、引用行都按整页居中，标题大字（29 号），
  引用行下面画一条通栏横线（从左边 10 毫米画到右边 10 毫米，穿过右边的笔记区）；
  正文宋体 12 号、段首空两格，正文栏从左边 11 毫米起、宽约 144 毫米，
  右边留约 5.5 厘米空白当笔记区。
- 正文按中文排版习惯处理：两端对齐；标点粘住前后字，不让标点单独成行、
  不让句号、逗号等标点出现在行首，也不让左引号、左括号出现在行末。
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

# 中文排版禁则：排版库自带的禁则表是按日文列的，缺了中文全角标点
# （，。；：？！”’》这些不在表里），而且它只管“行首”，不管“行末”，
# 连续的闭标点（比如 。”）断在行当中也管不了。所以不在库里修，
# 改成自己先断行：按中文习惯把每段文字预先断成行再交给排版库。
# 行首禁则：不能出现在行首的标点
# （破折号、省略号也算：它们不能拆开、不能出现在行首，行末出现是规范的）
_CLOSING = set("，。、；：？！…—”’）】》〉」』〗〕％%．.,;:?!)]")
# 行末禁则：不能出现在行末的标点（左引号、左括号这类）
_OPENING = set("“‘（［｛【《〈「『〖〔([{")
# 连字号：人名中间的 ·、数字之间的 ～，前后都不能断开
_BOTH = set("·～~")

# 宽度缓存：按（单元，字号）记，避免反复量同一批字
_WIDTH_CACHE = {}


def _unit_width(unit, font_size):
    """量一个单元在指定字号下的宽度（pt），和排版库用的同一把尺子。"""
    key = (unit, font_size)
    w = _WIDTH_CACHE.get(key)
    if w is None:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        try:
            w = stringWidth(unit, "NotoSerifSC", font_size)
        except Exception:
            w = font_size * len(unit)
        if not w or w <= 0:
            w = font_size * len(unit)
        _WIDTH_CACHE[key] = w
    return w


def _tokenize(text):
    """把文字切成不可拆的单元：连续的英文字母/数字（含小数点、百分号、
    词中的撇号，比如 25%、5.5、C919、don't）算一个单元，拆开就不好读；
    其他字（汉字、标点）各算一个单元。"""
    units = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isascii() and ch.isalnum():
            j = i
            while j < n:
                c = text[j]
                if c.isascii() and c.isalnum():
                    j += 1
                elif c in ".’'" and j + 1 < n and text[j + 1].isascii() and text[j + 1].isalnum():
                    j += 1  # 小数点、词中撇号留在词里
                elif c == "%":
                    j += 1
                    break
                else:
                    break
            units.append(text[i:j])
            i = j
        else:
            units.append(ch)
            i += 1
    return units


def split_lines(text, font_size, width, first_width=None, margin=0.05):
    """按中文排版习惯把一段文字预先断成行，返回行列表：

    - 两端对齐由排版库完成，这里只负责断行位置；每行留 0.05pt 余量，
      交给排版库时不会再被二次断行（余量要小，不然一行会少排一个字）。
    - 行要排满：不断行不断出来的行都排到栏宽，两端自然对齐；只有
      段末行可以短。闭标点放不下时允许它悬挂在栏外（中文排版里标点
      悬挂是规范做法），不断行就不会出现参差不齐的右沿。
    - 行首禁则：，。、；：？！等闭标点不能出现在行首；放不下的闭标点
      悬挂在上一行行末（连续的闭标点比如 。”）整个带下去，不拆开。
      破折号、省略号不能拆开、不能出现在行首，行末出现是规范的。
    - 行末禁则：“‘（等开标点不能留在行末；断行时把它留到下一行开头。
      人名中间的 ·、数字之间的 ～ 前后都不能断开，整组一起带下去。
    - 上面几条也保证了标点不会单独成行。
    width 是正文行的可用宽度（pt），first_width 是首行可用宽度
    （有首行缩进时比 width 小）；margin 是留给排版库的余量。"""
    if first_width is None:
        first_width = width
    units = _tokenize(text)
    widths = [_unit_width(u, font_size) for u in units]
    n = len(units)
    lines, i = [], 0
    first = True
    while i < n:
        avail = (first_width if first else width) - margin
        first = False
        j, used = i, 0.0
        end = None
        while j < n:
            u, w = units[j], widths[j]
            if u in _BOTH and j > i and j + 1 < n:
                # “字·字” / “数～数” 是一组，不能从中间断开：这个字和下
                # 一个字都得放得下，否则把前一个字一起带到下一行
                if used + w + widths[j + 1] > avail:
                    end = j - 1 if j - 1 > i else j
                    break
            if used + w > avail:
                break
            used += w
            j += 1
        if end is None:
            if j >= n:
                lines.append("".join(units[i:]))
                break
            nxt = units[j]
            if len(nxt) == 1 and nxt in _CLOSING:
                # 放不下的闭标点：如果它前后还连着闭标点（比如 。” 或 ……），
                # 悬挂一个会把另一个留在行首（排版库一次最多只悬挂一个），
                # 改成把行尾最后一个非标点字连同这串闭标点一起带到下一行；
                # 单个闭标点就让它悬挂在行末，行保持排满
                prev_closing = j > i and len(units[j - 1]) == 1 and units[j - 1] in _CLOSING
                next_closing = j + 1 < n and len(units[j + 1]) == 1 and units[j + 1] in _CLOSING
                if prev_closing or next_closing:
                    k = j - 1
                    while k > i and len(units[k]) == 1 and units[k] in _CLOSING:
                        k -= 1
                    end = k if k > i else j
                else:
                    end = j + 1  # 悬挂：把这个标点也算进本行（超宽一点）
            else:
                # 普通单元放不下，直接在这里断行；开标点也在这里断行，
                # 让它去下一行开头
                end = j
        # 行末禁则：行尾不能是开标点，把它留到下一行开头
        while end > i + 1 and len(units[end - 1]) == 1 and units[end - 1] in _OPENING:
            end -= 1
        lines.append("".join(units[i:end]))
        i = end
    return lines


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


def build_pdf(date, articles, font_path, out_path, subhead_font_path=None):
    """把文章列表生成 PDF，版面和之前的文章汇总版一致：

    - 没有封面，第一页就是目录：标题“人民日报文章目录”，条目按网站顺序连续编号，
      有正文的条目可以点、点进去跳到对应文章。
    - 正文每篇文章另起一页：标题大字居中，作者和引用行居中，横线下面是正文；
      正文宋体 12 号、段首空两格，右边留约 6 厘米空白当笔记区。
    - 正文里没有标点、字又少的短行是小标题，单独一行、居中、用黑体（粗体）排。
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
        Flowable,
        Frame,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
    )

    if not os.path.isfile(font_path):
        logging.error(f"找不到中文字体文件：{font_path}，请先下载 Noto Serif SC 再运行")
        return False
    pdfmetrics.registerFont(TTFont("NotoSerifSC", font_path))
    # 小标题用黑体（Noto Sans SC 粗体）：给了字体文件就注册来用，没给就
    # 退回宋体，不影响正文生成。
    subhead_font = "NotoSerifSC"
    if subhead_font_path and os.path.isfile(subhead_font_path):
        pdfmetrics.registerFont(TTFont("NotoSansSC-Bold", subhead_font_path))
        subhead_font = "NotoSansSC-Bold"
    elif subhead_font_path:
        logging.warning(f"找不到小标题字体文件：{subhead_font_path}，小标题改用宋体")

    # 给排版库的禁则表打补丁：它自带的表是按日文列的，缺了中文全角标点
    # （，。；：？！”’》这些）。预断行留出的悬挂标点偶尔会让一行超宽一点，
    # 排版库二次断行时靠这张表把标点留在行末，而不是推到下一行行首。
    # paragraph.py 是 import 绑定的名字，两个模块都要打。
    from reportlab.lib import textsplit as _textsplit
    import reportlab.platypus.paragraph as _paragraph_mod
    _extra_cannot_start = "，；：？！”’》〉」』〗〕·…—～％%"
    _patched = "".join(sorted(set(_textsplit.ALL_CANNOT_START) | set(_extra_cannot_start)))
    _textsplit.ALL_CANNOT_START = _patched
    _paragraph_mod.ALL_CANNOT_START = _patched

    black = HexColor("#000000")
    date_str = date.strftime("%Y-%m-%d")
    # 版面尺寸（按原版样例 PDF 实测）：
    # 目录页通栏；正文页的正文栏从左边 11 毫米起、宽约 144 毫米，右边留约 5.5 厘米空白当笔记区；
    # 但标题、作者、引用行和横线是按整页排的——标题按整页居中，横线从左边 10 毫米画到右边 10 毫米。
    # 注意：Frame 默认四边各有 6pt 内边距，内容实际从框线往里 6pt 开始画；
    # 这里全设成 0，让内容就从上面算好的位置开始，和原版量出来的尺寸对得上。
    toc_frame = Frame(25 * mm, 15 * mm, A4[0] - 50 * mm, A4[1] - 30 * mm, id="toc",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    body_frame = Frame(11 * mm, 19 * mm, 144 * mm, A4[1] - 38 * mm, id="body",
                       leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    # 正文栏左缘 11 毫米，右缘 155 毫米；通栏横线从 10 毫米画到 200 毫米
    frame_left_pt = 11 * mm
    frame_right_pt = frame_left_pt + 144 * mm
    rule_left = 28.35 - frame_left_pt
    rule_right = frame_right_pt - 566.93

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
    # 标题、作者、引用行按整页居中：用负缩进把段落框从正文栏扩到通栏（10 毫米到 200 毫米），
    # 这样居中的中心点就是整页中心，和原版一样。
    title_style = ParagraphStyle("标题", fontName="NotoSerifSC", fontSize=29, leading=38,
                                 alignment=TA_CENTER, spaceAfter=10, wordWrap="CJK",
                                 leftIndent=rule_left, rightIndent=rule_right)
    author_style = ParagraphStyle("作者", fontName="NotoSerifSC", fontSize=10, leading=14,
                                  alignment=TA_CENTER, spaceAfter=4, wordWrap="CJK",
                                  leftIndent=rule_left, rightIndent=rule_right)
    citation_style = ParagraphStyle("引用行", fontName="NotoSerifSC", fontSize=10, leading=14,
                                    alignment=TA_CENTER, spaceAfter=8, wordWrap="CJK",
                                    leftIndent=rule_left, rightIndent=rule_right)
    body_style = ParagraphStyle("正文", fontName="NotoSerifSC", fontSize=12, leading=21,
                                alignment=TA_JUSTIFY, firstLineIndent=24, spaceAfter=8,
                                wordWrap="CJK")
    # 正文里没有标点、字又少的短行是小标题，单独一行、居中、用黑体排
    # （比如“领航定向 驾驭发展全局创造新的历史伟业”）
    subhead_style = ParagraphStyle("小标题", fontName=subhead_font, fontSize=12, leading=21,
                                   alignment=TA_CENTER, spaceBefore=6, spaceAfter=6,
                                   wordWrap="CJK")

    class FullWidthRule(Flowable):
        """引用行下面的通栏横线：从页面左边 10 毫米画到右边 10 毫米，
        穿过右边的笔记区，和原版一样。画在正文栏里，但线伸到栏外——
        排版框不会裁剪画出去的内容，所以这样画没问题。"""

        def __init__(self, x0, x1):
            super().__init__()
            self.x0, self.x1 = x0, x1

        def wrap(self, avail_width, avail_height):
            # 占 12pt 高：上面留 2pt、线 0.5pt、下面留约 10pt 再接正文
            return (avail_width, 12)

        def draw(self):
            self.canv.saveState()
            self.canv.setStrokeColor(black)
            self.canv.setLineWidth(0.5)
            self.canv.line(self.x0, 9.5, self.x1, 9.5)
            self.canv.restoreState()

    # 预断行用的宽度（pt）：正文栏 144 毫米；首行要减掉 24pt 首行缩进；
    # 标题、作者、引用行按整页排，可用宽度是通栏 10 毫米到 200 毫米；
    # 目录栏是整页减去左右各 25 毫米。
    body_width_pt = 144 * mm
    body_first_width_pt = body_width_pt - 24
    full_width_pt = 566.93 - 28.35
    toc_width_pt = A4[0] - 50 * mm

    _style_seq = [0]

    def para_flowables(text, font_size, width, style, first_width=None, link=None, anchor=None):
        """一段文字按中文习惯预先断行，每行一个独立段落交给排版库。

        不能把几行用 <br/> 拼成一个段落：悬挂标点会让某一行超宽一点，
        排版库二次断行时会在那行后面多留出一整行的空行，行间距就乱了。
        每行单独成段就不会有这个毛病——单行段落二次断行只会把标点
        悬挂在行末，不会多出一行。段前段后的空白只加在整段的第一行
        和最后一行，行与行之间只按行距排。"""
        lines = split_lines(text, font_size, width, first_width)
        out = []
        n = len(lines)
        for k, line in enumerate(lines):
            kw = {
                "spaceBefore": style.spaceBefore if k == 0 else 0,
                "spaceAfter": style.spaceAfter if k == n - 1 else 0,
            }
            if k > 0:
                kw["firstLineIndent"] = 0
            _style_seq[0] += 1
            line_style = ParagraphStyle(f"{style.name}-行{_style_seq[0]}", parent=style, **kw)
            body = html.escape(line)
            if link:
                body = f'<link href="{link}">{body}</link>'
            if anchor and k == 0:
                body = f'<a name="{anchor}"/>{body}'
            out.append(Paragraph(body, line_style))
        return out

    story = []
    # 目录：按网站顺序连续编号，只列标题，不标页码；有正文的条目做成可点链接
    story.append(Paragraph("人民日报文章目录", toc_title_style))
    for i, (_, title, _, _, paragraphs) in enumerate(articles, 1):
        link = f"#art{i}" if paragraphs else None
        story.extend(para_flowables(f"{i}. {title}", 12, toc_width_pt, toc_item_style, link=link))

    # 正文：每篇文章另起一页
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    for i, (_, title, author, citation, paragraphs) in enumerate(articles, 1):
        if not paragraphs:
            continue
        if i > 1:
            story.append(PageBreak())
        # 标题里放一个锚点，目录点进来就跳到这里
        story.extend(para_flowables(title, 29, full_width_pt, title_style, anchor=f"art{i}"))
        if author:
            story.extend(para_flowables(author, 10, full_width_pt, author_style))
        if citation:
            story.extend(para_flowables(citation, 10, full_width_pt, citation_style))
        story.append(FullWidthRule(rule_left, 566.93 - frame_left_pt))
        for text in paragraphs:
            plain = text.replace(" ", "").replace("　", "")
            if len(plain) <= 22 and not any(c in text for c in "。！？；，：、…"):
                story.extend(para_flowables(text, 12, body_width_pt, subhead_style))
            else:
                story.extend(para_flowables(text, 12, body_width_pt, body_style, body_first_width_pt))

    doc.build(story)
    logging.info(f"文章汇总版 PDF 已生成：{out_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="抓取人民日报当日文章，生成文章汇总版 PDF")
    parser.add_argument("--date", default=None,
                        help="指定日期，格式 YYYY-MM-DD；不指定则自动识别当天（报纸没出就回退到昨天）")
    parser.add_argument("--font", default="NotoSerifSC.ttf",
                        help="中文字体文件路径，默认当前目录下的 NotoSerifSC.ttf")
    parser.add_argument("--subhead-font", default=None,
                        help="小标题黑体文件路径，不指定则小标题用宋体")
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
    if not build_pdf(date, articles, args.font, out_path, args.subhead_font):
        return 1
    with_body = sum(1 for a in articles if a[4])
    logging.info(f"文章汇总版完成：{date_str}，目录 {len(articles)} 条、正文 {with_body} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
