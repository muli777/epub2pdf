# EPUB 转 PDF

选择某个具体的 EPUB 文件，或选择一个文件夹批量转换其中所有 EPUB 文件为 PDF。

## 转换引擎（自动按优先级选择）

1. **Calibre（最佳）** — 若安装了 Calibre，调用 `ebook-convert`，质量最高。
2. **无头浏览器（Chromium 内核）** — 检测到 Chrome/Edge/Brave 即用其无头模式逐章渲染再合并。
   质量接近浏览器，**表格、上下标、图片、MathML/MathJax 公式、复杂 CSS 都能正确渲染**。
   无需额外安装，Windows 自带的 Edge 即可。
3. **纯 Python 兜底** — 都没有时用 `ebooklib` + `xhtml2pdf`，表格/公式支持有限，仅作最后手段。

## 安装依赖（仅需纯 Python 回退方案时）

```bash
pip install -r requirements.txt
```

> 推荐额外安装 [Calibre](https://calibre-ebook.com/)（免费），脚本会自动检测并优先使用，效果更好。

## 使用方式

### GUI 模式（无参数）

```bash
python epub2pdf.py
```

- 点击「选择 EPUB 文件」选择单个/多个文件；
- 或点击「选择文件夹(批量)」转换文件夹下所有 `.epub`；
- 可选「输出目录」（默认与源文件同目录）；
- 点击「开始转换」，日志区显示进度。

### 命令行模式

```bash
# 转换单个文件
python epub2pdf.py "D:\books\novel.epub"

# 批量转换文件夹下所有 EPUB
python epub2pdf.py "D:\books" -o "D:\out"
```

## 说明

- 纯 Python 方案对排版复杂的 EPUB（复杂 CSS、特殊字体）支持有限，如效果不理想请安装 Calibre。
- 输出文件名 = EPUB 文件名 + `.pdf`，若已存在会被覆盖。
