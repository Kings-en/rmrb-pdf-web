# 人民日报自动下载器（手机网页版）
> 这是一个用于自动获取每日人民日报完整PDF的项目，仅限自己学习使用，若有侵权联系删除。

这是一个用于自动下载并合并人民日报版面 PDF 的 Python 程序。该程序每天定时从人民日报的官方网站下载最新的 PDF 文件，并将它们合并为一个完整的 PDF 文件。
两个分支，其中`main`提供PDF版报纸下载功能，`article-crawler`提供PDF版文章汇总下载，可用于打印后做笔记使用或导入ipad笔记软件使用。

## 手机上怎么用（GitHub Pages 网页版）

不用安装 App，不用过应用商店审核，苹果、安卓、华为手机用浏览器打开就能用：

1. 把这份代码推到 GitHub 仓库（就是这个仓库本身）。
2. 在仓库里打开 **Settings → Pages**，Source 选 **Deploy from a branch**，Branch 选 **main**，文件夹选 **/(root)**，点 Save。
3. 等一两分钟，打开 `https://kings-en.github.io/rmrb-pdf-web/` 就能看到手机版页面：最新一期在最上面，可以按日期查、在线查看、下载 PDF。
4. 在手机浏览器里点“添加到主屏幕”，以后就像 App 一样打开。

每天更新是怎么来的：`.github/workflows/daily.yml` 每天北京时间 08:00（UTC 00:00）自动跑一次，先跑 `python rmrb_download.py --once` 抓完整版 PDF，再跑 `python article_crawler.py` 抓当日全部版面文章、生成文章汇总版 PDF，两个都归档到 `pdfs/`（完整版 `YYYY-MM-DD.pdf`，文章汇总版 `YYYY-MM-DD-article.pdf`），再用 `generate_index.py` 更新 `index.json`，提交回仓库，网页随即更新。也可以在仓库 **Actions** 页面点 **Run workflow** 手动跑一次。

## 文章汇总版（来自 article-crawler 分支）

原仓库 `article-crawler` 分支的功能也已经接进来了，每天和完整版一起自动生成：

- `article_crawler.py`：自动识别当天日期（当天报纸还没出就回退到昨天），抓全部版面的文章链接，多线程下载每篇文章的正文，再用 reportlab 生成一份带目录和页码的 PDF，存在 `人民日报下载/人民日报-YYYY-MM-DD-文章汇总版.pdf`，工作流归档为 `pdfs/YYYY-MM-DD-article.pdf`，网页上显示为“文章汇总版”。
- 网页上同一天会同时列出完整版和文章汇总版，最新一期优先显示完整版。
- 注意：原分支里那个旧脚本（`rmrb_merged.py`）有两个问题，这里都修了：它把日期写死成 2025-10-19，只能抓那一天；而且只抓头版一个版面、只保存成 txt，不生成 PDF。现在的版本按上面这套逻辑重写过。
- 文章汇总版排版用的中文字体是 Noto Sans SC，工作流每次运行时下载（`NotoSansSC.ttf`），本地跑之前也要先准备好字体文件，见 `article_crawler.py` 开头的说明。
- 文章汇总那一步如果失败，不会拖垮整个任务：完整版照样归档提交，失败原因可以在那次运行的日志里看到。

注意：
- GitHub Pages 只能放静态网页，爬虫本身不在网页里跑，是在 GitHub Actions 里跑的。
- 原脚本本地运行时是死循环定时模式；加了 `--once` 参数后才适合在 Actions 里单次运行，本地用法不变。
- 仓库里的 `pdfs/` 一开始是空的，第一次定时任务跑完后才会有当天的报纸。

## 功能

- 自动获取人民日报最新页面。
- 下载并合并该页面中的所有 PDF 文件。
- 每天早上8点定时执行任务。
- 支持日志记录和错误处理。
- 手机网页：按日期浏览、在线查看、下载 PDF。
- 每天自动生成文章汇总版 PDF：当日全部版面文章正文合并成一份，带目录和页码。

## 安装（本地运行爬虫）

1. 克隆项目到本地：

   ```bash
   git clone https://github.com/Kings-en/rmrb-pdf-web.git
   cd rmrb-pdf-web
   ```

2. 安装依赖库：

   ```bash
   pip install -r requirements.txt
   ```

3. 确保你的系统上已经安装了 `Google Chrome`，驱动由 Selenium 自带的驱动管理器自动下载匹配版本，不用再手动装 ChromeDriver，也不用 webdriver-manager。

## 配置

1. 打开 `rmrb_download.py`，检查下载目录和日志文件位置，确保它们适合你的需求。
2. 你可以根据需要修改下载时间，程序默认每天早上 8 点运行。

## 使用

1. 运行程序：

   ```bash
   python rmrb_download.py
   ```

2. 程序将会开始运行并每天自动执行下载任务。
3. 查看日志文件 rmrb_download.log，可以查看每次下载任务的详细日志。

只想跑一次（测试 / GitHub Actions 用）：

```bash
python rmrb_download.py --once
```

只跑文章汇总版（测试用，字体文件要先准备好）：

```bash
python article_crawler.py --font ./NotoSansSC.ttf
```

## 文件说明

- `index.html`：手机网页（最新一期、按日期查询、在线查看、下载）。
- `index.json`：报纸目录，由 `generate_index.py` 生成，网页读取它来列出报纸。
- `generate_index.py`：扫描 `pdfs/` 生成 `index.json`。
- `pdfs/`：每日完整版 PDF（`YYYY-MM-DD.pdf`）和文章汇总版 PDF（`YYYY-MM-DD-article.pdf`）。
- `article_crawler.py`：文章汇总版爬虫，抓当日全部版面文章正文并生成汇总 PDF。
- `manifest.webmanifest` / `icon.svg`：添加到主屏幕用的配置和图标。
- `.github/workflows/daily.yml`：每天北京时间 08:00 自动抓取并提交。
- `rmrb_download.py`：原爬虫，新增 `--once` 单次运行模式。

## 贡献

如果你希望为该项目贡献代码，可以创建一个 pull request，提交新的功能或修复 bug。
