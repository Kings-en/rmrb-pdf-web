#!/usr/bin/env python3
"""把当天生成的 PDF 上传到七牛云 Kodo。

环境变量（在 GitHub Actions 里用仓库 Secrets 传入，不要写进代码、不要发在聊天里）：
    QINIU_ACCESS_KEY  七牛 AccessKey
    QINIU_SECRET_KEY  七牛 SecretKey
    QINIU_BUCKET      存储空间名，例如 rmrb-pdf
    QINIU_DOMAIN      对外访问域名，例如 https://file.aonote.cn（不要带末尾斜杠）

上传规则：
- 本地 pdfs/2026-11-05.pdf -> 七牛 key 为 pdfs/2026-11-05.pdf
- 本地 pdfs/2026-11-05-article.pdf -> 七牛 key 为 pdfs/2026-11-05-article.pdf
- 同名文件直接覆盖上传（每天只跑一次，不会有冲突）

本地用法（先在七牛控制台建好空间、拿到密钥再跑）：
    QINIU_ACCESS_KEY=xxx QINIU_SECRET_KEY=xxx QINIU_BUCKET=rmrb-pdf \
        python upload_to_qiniu.py
"""
import os
import sys

PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfs")


def main():
    access_key = os.environ.get("QINIU_ACCESS_KEY", "")
    secret_key = os.environ.get("QINIU_SECRET_KEY", "")
    bucket = os.environ.get("QINIU_BUCKET", "")
    if not access_key or not secret_key or not bucket:
        print("缺少七牛密钥或空间名：请设置 QINIU_ACCESS_KEY、QINIU_SECRET_KEY、QINIU_BUCKET")
        return 1

    try:
        import qiniu
    except ImportError:
        print("没有安装 qiniu 库：请先 pip install qiniu")
        return 1

    auth = qiniu.Auth(access_key, secret_key)
    uploaded = []
    if not os.path.isdir(PDF_DIR):
        print(f"没有找到 {PDF_DIR}，没有可上传的文件")
        return 0

    for name in sorted(os.listdir(PDF_DIR)):
        if not name.endswith(".pdf"):
            continue
        local_path = os.path.join(PDF_DIR, name)
        key = f"pdfs/{name}"
        token = auth.upload_token(bucket, key)
        ret, info = qiniu.put_file(token, key, local_path)
        if info.status_code != 200:
            print(f"上传失败 {name}：{info.status_code} {info.text_body}")
            return 1
        size_mb = os.path.getsize(local_path) / 1024 / 1024
        print(f"已上传 {name}（{size_mb:.1f} MB）-> {key}")
        uploaded.append(name)

    if not uploaded:
        print("pdfs/ 里没有 PDF，没有上传任何文件")
    else:
        print(f"共上传 {len(uploaded)} 个文件到空间 {bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
