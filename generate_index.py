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
    entries = []
    if os.path.isdir(PDF_DIR):
        for name in sorted(os.listdir(PDF_DIR)):
            m = FILENAME_RE.match(name)
            if not m:
                continue
            date_str, article = m.group(1), m.group(2)
            path = os.path.join(PDF_DIR, name)
            size = os.path.getsize(path)
            entries.append({
                "date": date_str,
                "kind": "文章汇总版" if article else "完整版",
                "file": f"pdfs/{name}",
                "size_bytes": size,
                "size": f"{size / 1024 / 1024:.1f} MB",
                "pages": page_count(path),
            })

    entries.sort(key=lambda e: e["date"], reverse=True)
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
