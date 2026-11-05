#!/usr/bin/env python3
"""扫描 pdfs/ 目录，生成供手机网页读取的 index.json。

文件名约定：
- pdfs/2025-10-19.pdf            -> 完整版
- pdfs/2025-10-20-article.pdf    -> 文章汇总版

本地和 GitHub Actions 都可以直接跑：
    python generate_index.py
"""
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta

PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfs")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.json")

FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(-article)?\.pdf$")


def page_count(path):
    try:
        from PyPDF2 import PdfReader
        with open(path, "rb") as f:
            return len(PdfReader(f).pages)
    except Exception:
        return None


def main():
    # 七牛域名（例如 https://file.aonote.cn）：设置了就用七牛地址，
    # 没设置就用原来的相对地址 pdfs/xxx.pdf（GitHub Pages）。
    # 下载地址单独加 ?attname=文件名：七牛收到这个参数会返回
    # Content-Disposition: attachment，Safari 点了就直接下载，
    # 不用再像以前那样先把整个文件取回来再保存。
    domain = os.environ.get("QINIU_DOMAIN", "").rstrip("/")
    entries = []
    if os.path.isdir(PDF_DIR):
        for name in sorted(os.listdir(PDF_DIR)):
            m = FILENAME_RE.match(name)
            if not m:
                continue
            date_str, article = m.group(1), m.group(2)
            path = os.path.join(PDF_DIR, name)
            size = os.path.getsize(path)
            if domain:
                file_url = f"{domain}/pdfs/{name}"
                download_url = file_url + "?attname=" + urllib.parse.quote(name)
            else:
                file_url = f"pdfs/{name}"
                download_url = file_url
            entries.append({
                "date": date_str,
                "kind": "文章汇总版" if article else "完整版",
                "file": file_url,
                "download": download_url,
                "size_bytes": size,
                "size": f"{size / 1024 / 1024:.1f} MB",
                "pages": page_count(path),
            })

    # 合并旧目录：PDF 不再提交进 Git 仓库之后，pdfs/ 里只有当天的
    # 文件，旧的日期要靠上一次的 index.json 留下来，不然列表会丢。
    old_entries = []
    if os.path.isfile(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old_entries = json.load(f).get("entries", [])
        except Exception:
            old_entries = []
    seen = {(e["date"], e["kind"]) for e in entries}
    for e in old_entries:
        if (e.get("date"), e.get("kind")) not in seen:
            entries.append(e)

    entries.sort(key=lambda e: (e["date"], 1 if e["kind"] == "完整版" else 0), reverse=True)
    beijing = timezone(timedelta(hours=8))
    data = {
        "generated_at": datetime.now(beijing).strftime("%Y-%m-%d %H:%M"),
        "timezone": "Asia/Shanghai",
        "entries": entries,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"写入 {OUT}，共 {len(entries)} 条")
    for e in entries:
        print(f"  {e['date']} {e['kind']} {e['file']} {e['size']}")


if __name__ == "__main__":
    main()
