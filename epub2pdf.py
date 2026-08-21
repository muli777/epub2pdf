#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epub2pdf — EPUB 转 PDF 转换器

功能：
  - 选择「某个具体的 EPUB 文件」转换为 PDF
  - 选择「某个文件夹」批量转换其中的所有 EPUB 文件为 PDF
  - 输出目录可选，默认与源文件同目录

转换引擎：
  1. 优先使用 Calibre 的 ebook-convert（质量最佳）
  2. 若未安装 Calibre，则回退到纯 Python 方案（ebooklib + xhtml2pdf）

用法：
  GUI 模式（双击或无参数运行）:
      python epub2pdf.py

  命令行模式:
      python epub2pdf.py <文件或文件夹> [-o 输出目录]
"""

from __future__ import annotations

import argparse
import base64
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional

PDF_SUFFIX = ".pdf"
EPUB_SUFFIX = ".epub"


# ============================== 引擎检测与调用 ==============================

def find_calibre() -> Optional[str]:
    """检测 Calibre 的 ebook-convert 可执行文件路径，找不到返回 None。"""
    exe = shutil.which("ebook-convert")
    if exe:
        return exe

    # Windows 常见安装路径
    candidates = []
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LOCALAPPDATA", "")
    for base in (pf, pf86, local_app):
        if base:
            candidates.append(os.path.join(base, "Calibre2", "ebook-convert.exe"))
            candidates.append(os.path.join(base, "calibre", "ebook-convert.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def convert_with_calibre(ebook_convert: str, epub_path: Path, pdf_path: Path,
                         log: Callable[[str], None]) -> bool:
    """调用 Calibre 进行转换。成功返回 True。"""
    cmd = [ebook_convert, str(epub_path), str(pdf_path),
           "--paper-size", "a4", "--pdf-page-margin-left", "72",
           "--pdf-page-margin-right", "72"]
    log(f"[Calibre] 转换中: {epub_path.name}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    except Exception as e:
        log(f"[Calibre] 调用失败: {e}")
        return False
    if proc.returncode != 0:
        log(f"[Calibre] 转换失败 (返回码 {proc.returncode})")
        if proc.stderr:
            log(proc.stderr[-1500:])
        return False
    if proc.stdout:
        log(proc.stdout[-800:])
    return True


# ---------------------- 无头浏览器引擎（Chromium 内核）----------------------
# 真正的浏览器内核，能正确渲染表格、上下标、图片、MathML/MathJax 公式
# 与复杂 CSS。优先级仅次于 Calibre。

def find_browser() -> Optional[str]:
    """检测 Chromium 内核浏览器可执行文件，找不到返回 None。"""
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    # 按优先级：Chrome -> Edge -> Brave
    patterns = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for pat in patterns:
        for base in (pf, pf86, local):
            if not base:
                continue
            p = os.path.join(base, pat)
            if os.path.isfile(p):
                return p
    # 也尝试 PATH 中的命令
    for name in ("chrome", "chromium", "msedge", "brave"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def convert_with_chromium(browser_exe: str, epub_path: Path, pdf_path: Path,
                          log: Callable[[str], None]) -> bool:
    """用无头浏览器逐章渲染 EPUB 并合并为单个 PDF。"""
    import tempfile
    import zipfile
    from pypdf import PdfWriter  # type: ignore
    from ebooklib import epub, ITEM_DOCUMENT  # type: ignore

    label = Path(browser_exe).stem  # chrome / msedge / brave
    log(f"[{label}] 解压 EPUB 并按阅读顺序逐章渲染")

    with tempfile.TemporaryDirectory(prefix="epub2pdf_") as workdir:
        work = Path(workdir)
        try:
            with zipfile.ZipFile(epub_path) as z:
                z.extractall(work)
        except Exception as e:
            log(f"[{label}] 解压失败: {e}")
            return False

        # 取得 spine（阅读顺序）对应的文件路径
        try:
            book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        except Exception:
            book = None

        ordered_names: List[str] = []
        if book is not None:
            for idref, _linear in book.spine:
                it = book.get_item_with_id(idref)
                if it is not None and it.get_name().lower().endswith(
                        (".xhtml", ".html", ".htm")):
                    ordered_names.append(it.get_name())
        if not ordered_names and book is not None:
            ordered_names = [it.get_name() for it in book.get_items_of_type(ITEM_DOCUMENT)]

        # ebooklib 的 get_name() 返回 manifest 里的 href（可能是基名），
        # 不一定是 zip 内真实路径。这里解析 OPF 目录把 href 还原为真实路径。
        opf_dir = ""
        container = work / "META-INF" / "container.xml"
        if container.exists():
            m = re.search(r'full-path="([^"]+)"',
                          container.read_text(encoding="utf-8", errors="replace"))
            if m:
                opf_dir = posixpath.dirname(m.group(1))

        basename_index: dict = {}
        for p in work.rglob("*"):
            if p.suffix.lower() in (".xhtml", ".html", ".htm"):
                basename_index.setdefault(p.name, p.relative_to(work).as_posix())

        def _resolve_chapter(href: str) -> Optional[str]:
            href = urllib.parse.unquote(href.split("#")[0])
            cand = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
            if (work / cand).exists():
                return cand
            base = posixpath.basename(href)
            return basename_index.get(base)

        resolved: List[str] = []
        for href in ordered_names:
            r = _resolve_chapter(href)
            if r:
                resolved.append(r)
        # 去重保序
        seen = set()
        ordered_names = [n for n in resolved
                         if not (n in seen or seen.add(n))]

        if not ordered_names:
            # 退路：扫描解压目录下所有 xhtml
            ordered_names = [str(p.relative_to(work)) for p in
                             sorted(work.rglob("*.xhtml"))]

        profile_dir = work / "profile"
        chapter_pdfs: List[Path] = []
        total = len(ordered_names)
        for i, name in enumerate(ordered_names, 1):
            src = work / name
            if not src.exists():
                continue
            out_pdf = work / f"_chapter_{i:04d}.pdf"
            log(f"[{label}] ({i}/{total}) 渲染 {name}")
            cmd = [
                browser_exe,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--allow-file-access-from-files",
                "--virtual-time-budget=10000",
                f"--user-data-dir={profile_dir}",
                f"--print-to-pdf={out_pdf}",
                src.as_uri(),
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=180)
            except Exception as e:
                log(f"  调用失败: {e}")
                continue
            # Chrome 退出后偶尔需要一小段缓冲才落盘
            if not (out_pdf.exists() and out_pdf.stat().st_size > 0):
                import time
                for _ in range(20):
                    time.sleep(0.1)
                    if out_pdf.exists() and out_pdf.stat().st_size > 0:
                        break
            if out_pdf.exists() and out_pdf.stat().st_size > 0:
                chapter_pdfs.append(out_pdf)
            else:
                log(f"  未生成 PDF: {name}")
                if proc.stderr:
                    log(f"  {proc.stderr.strip()[-400:]}")

        if not chapter_pdfs:
            log(f"[{label}] 没有任何章节渲染成功")
            return False

        log(f"[{label}] 合并 {len(chapter_pdfs)} 个章节 -> {pdf_path.name}")
        try:
            writer = PdfWriter()
            for p in chapter_pdfs:
                writer.append(str(p))
            with open(pdf_path, "wb") as f:
                writer.write(f)
        except Exception as e:
            log(f"[{label}] 合并失败: {e}")
            return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def resolve_engine() -> Optional[dict]:
    """按优先级返回可用引擎：Calibre > 无头浏览器 > None(纯Python)。"""
    cal = find_calibre()
    if cal:
        return {"type": "calibre", "exe": cal, "name": "Calibre"}
    br = find_browser()
    if br:
        return {"type": "chromium", "exe": br, "name": Path(br).stem}
    return None


# ============================== 纯 Python 回退方案 ==============================

def _resolve_href(base_name: str, href: str) -> str:
    """根据 xhtml 文件在 zip 中的路径解析相对链接，返回标准化后的 zip 路径。"""
    href = urllib.parse.unquote(href.split("#")[0])
    base_dir = posixpath.dirname(base_name)
    return posixpath.normpath(posixpath.join(base_dir, href))


def _inline_images(soup, book, base_name: str, image_index: dict) -> None:
    """将 <img src> / <image xlink:href> 指向的图片替换为 base64 data URI。"""
    # 普通 <img>
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith(("http://", "https://", "data:")):
            continue
        resolved = _resolve_href(base_name, src)
        item = image_index.get(resolved)
        if item is None:
            continue
        mime = getattr(item, "media_type", None) or "image/jpeg"
        b64 = base64.b64encode(item.content).decode("ascii")
        img["src"] = f"data:{mime};base64,{b64}"

    # SVG <image xlink:href>（封面常用）
    for img in soup.find_all("image"):
        href = img.get("xlink:href") or img.get("href")
        if not href or href.startswith(("http://", "https://", "data:")):
            continue
        resolved = _resolve_href(base_name, href)
        item = image_index.get(resolved)
        if item is None:
            continue
        mime = getattr(item, "media_type", None) or "image/jpeg"
        b64 = base64.b64encode(item.content).decode("ascii")
        img["xlink:href"] = f"data:{mime};base64,{b64}"


# xhtml2pdf 只能处理少数 CSS 函数；遇到 calc()/var()/clamp()/gradient() 等
# 现代 CSS 函数时会在 parser.py 内部做 obj[0] 下标而崩溃
# （报错 'CSSTerminalFunction' object is not subscriptable）。
# 这里只放行 rgb/rgba/hsl/hsla/url，其它含函数的声明一律剔除。
_ALLOWED_CSS_FUNCS = {"rgb", "rgba", "hsl", "hsla", "url"}
_FUNC_CALL_RE = re.compile(r"([A-Za-z][\w-]*)\s*\(")
_DECL_RE = re.compile(r"([A-Za-z][\w-]+)\s*:\s*([^;{}]+)\s*(?=;|$)")


def _has_unsupported_func(value: str) -> bool:
    for m in _FUNC_CALL_RE.finditer(value):
        if m.group(1).lower() not in _ALLOWED_CSS_FUNCS:
            return True
    return False


def _sanitize_style_block(css: str) -> str:
    """清洗 <style> 文本：删除含不支持函数的声明。"""
    if not css:
        return css

    def repl(m: re.Match) -> str:
        value = m.group(2)
        return "" if _has_unsupported_func(value) else m.group(0)

    return _DECL_RE.sub(repl, css)


def _sanitize_inline_style(style: str) -> str:
    """清洗内联 style="..."：删除含不支持函数的声明。"""
    if not style:
        return style
    kept = []
    for part in style.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        value = part.split(":", 1)[1]
        if _has_unsupported_func(value):
            continue
        kept.append(part)
    return "; ".join(kept)


def _sanitize_css(soup) -> None:
    """就地清洗 soup 中的 <style> 标签与内联 style 属性。"""
    for tag in soup.find_all("style"):
        text = tag.string or tag.get_text()
        tag.string = _sanitize_style_block(text)

    for tag in soup.find_all(attrs={"style": True}):
        cleaned = _sanitize_inline_style(tag.get("style", ""))
        if cleaned:
            tag["style"] = cleaned
        else:
            del tag["style"]


def _epub_to_html(epub_path: Path) -> str:
    """读取 EPUB，按 spine 顺序拼接章节 HTML，并把图片内联为 data URI。"""
    from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore

    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})

    image_index = {it.get_name(): it for it in book.get_items_of_type(ITEM_IMAGE)}

    # 按 spine 顺序取出文档项
    ordered: List = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is not None:
            ordered.append(item)
    if not ordered:
        ordered = list(book.get_items_of_type(ITEM_DOCUMENT))

    base_style = """
    <style>
      body { font-family: 'Noto Sans', 'Microsoft YaHei', 'SimSun', sans-serif;
             font-size: 12pt; line-height: 1.6; }
      h1, h2, h3 { page-break-after: avoid; }
      img { max-width: 100%; }
      .chapter-break { page-break-before: always; }
    </style>
    """

    parts: List[str] = []
    for idx, item in enumerate(ordered):
        try:
            raw = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        soup = BeautifulSoup(raw, "html.parser")
        _inline_images(soup, book, item.get_name(), image_index)
        _sanitize_css(soup)

        body = soup.find("body")
        content = str(body) if body else str(soup)
        cls = ' class="chapter-break"' if idx > 0 else ""
        parts.append(f'<div{cls}>{content}</div>')

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'{base_style}</head><body>{"".join(parts)}</body></html>'
    )


def convert_with_python(epub_path: Path, pdf_path: Path,
                        log: Callable[[str], None]) -> bool:
    """纯 Python 方案：ebooklib 解析 + xhtml2pdf 渲染。"""
    log(f"[Python] 解析 EPUB: {epub_path.name}")
    try:
        html = _epub_to_html(epub_path)
    except Exception as e:
        log(f"[Python] 解析失败: {e}")
        log(traceback.format_exc()[-1500:])
        return False

    try:
        from xhtml2pdf import pisa  # type: ignore
    except Exception as e:
        log(f"[Python] 缺少依赖 xhtml2pdf，请运行: pip install -r requirements.txt\n  {e}")
        return False

    log(f"[Python] 渲染 PDF: {pdf_path.name}")
    try:
        with open(pdf_path, "wb") as f:
            result = pisa.CreatePDF(html, dest=f, encoding="utf-8")
    except Exception as e:
        log(f"[Python] 渲染失败: {e}")
        log(traceback.format_exc()[-1500:])
        return False

    if getattr(result, "err", 0):
        log(f"[Python] 渲染过程中出现错误 (err={result.err})")
        return False
    return True


# ============================== 调度入口 ==============================

def convert_one(epub_path: Path, out_dir: Path, engine: Optional[dict],
                log: Callable[[str], None]) -> bool:
    """转换单个 EPUB。返回是否成功。

    engine 为 resolve_engine() 的结果（Calibre 或无头浏览器）；为 None 时
    使用纯 Python 方案兜底。
    """
    if not epub_path.is_file() or epub_path.suffix.lower() != EPUB_SUFFIX:
        log(f"跳过非 epub 文件: {epub_path}")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (epub_path.stem + PDF_SUFFIX)

    log("=" * 60)
    log(f"开始转换: {epub_path}")
    log(f"输出文件: {pdf_path}")

    if engine is None:
        ok = convert_with_python(epub_path, pdf_path, log)
    elif engine.get("type") == "calibre":
        ok = convert_with_calibre(engine["exe"], epub_path, pdf_path, log)
    elif engine.get("type") == "chromium":
        ok = convert_with_chromium(engine["exe"], epub_path, pdf_path, log)
    else:
        ok = convert_with_python(epub_path, pdf_path, log)

    if ok and pdf_path.exists():
        size_kb = pdf_path.stat().st_size / 1024
        log(f"✅ 完成: {pdf_path.name} ({size_kb:.1f} KB)")
        return True
    log(f"❌ 失败: {epub_path.name}")
    return False


def collect_epub_files(target: Path) -> List[Path]:
    """收集目标（文件或文件夹）下的所有 EPUB 文件。"""
    if target.is_file():
        return [target] if target.suffix.lower() == EPUB_SUFFIX else []
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.suffix.lower() == EPUB_SUFFIX)
    return []


# ============================== GUI ==============================

def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("EPUB 转 PDF")
    root.geometry("760x560")

    state = {"engine": resolve_engine(), "files": []}

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")

    def pick_file():
        files = filedialog.askopenfilenames(
            title="选择 EPUB 文件",
            filetypes=[("EPUB 文件", "*.epub"), ("所有文件", "*.*")],
        )
        if files:
            state["files"] = [Path(f) for f in files]
            refresh_list()

    def pick_folder():
        folder = filedialog.askdirectory(title="选择包含 EPUB 的文件夹")
        if folder:
            state["files"] = collect_epub_files(Path(folder))
            refresh_list()
            if not state["files"]:
                logbox.insert("end", f"该文件夹下未找到任何 .epub 文件: {folder}\n")

    def pick_outdir():
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            out_var.set(d)

    ttk.Button(top, text="选择 EPUB 文件", command=pick_file).pack(side="left", padx=4)
    ttk.Button(top, text="选择文件夹(批量)", command=pick_folder).pack(side="left", padx=4)
    top.pack(fill="x")

    list_frame = ttk.LabelFrame(root, text="待转换文件", padding=6)
    list_frame.pack(fill="both", expand=True, padx=10, pady=6)

    file_list = tk.Listbox(list_frame, height=10)
    file_list.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(list_frame, orient="vertical", command=file_list.yview)
    sb.pack(side="right", fill="y")
    file_list.config(yscrollcommand=sb.set)

    def refresh_list():
        file_list.delete(0, "end")
        for p in state["files"]:
            file_list.insert("end", str(p))

    out_frame = ttk.Frame(root, padding=(10, 0))
    out_frame.pack(fill="x")
    ttk.Label(out_frame, text="输出目录:").pack(side="left")
    out_var = tk.StringVar(value="")
    ttk.Entry(out_frame, textvariable=out_var).pack(side="left", fill="x",
                                                    expand=True, padx=6)
    ttk.Button(out_frame, text="浏览…", command=pick_outdir).pack(side="left")

    log_frame = ttk.LabelFrame(root, text="日志", padding=6)
    log_frame.pack(fill="both", expand=True, padx=10, pady=6)
    logbox = tk.Text(log_frame, height=10, wrap="none")
    logbox.pack(side="left", fill="both", expand=True)
    lsb = ttk.Scrollbar(log_frame, orient="vertical", command=logbox.yview)
    lsb.pack(side="right", fill="y")
    logbox.config(yscrollcommand=lsb.set)

    bottom = ttk.Frame(root, padding=10)
    bottom.pack(fill="x")
    progress = ttk.Progressbar(bottom, mode="determinate")
    progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
    convert_btn = ttk.Button(bottom, text="开始转换")
    convert_btn.pack(side="right")

    def log(msg: str):
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        root.update_idletasks()

    engine = state["engine"]
    if engine and engine.get("type") == "calibre":
        log(f"已检测到 Calibre: {engine['exe']}")
    elif engine and engine.get("type") == "chromium":
        log(f"将使用无头浏览器渲染（{engine['name']}），质量接近浏览器，"
            f"表格/上下标/公式/图片均可正确渲染。")
    else:
        log("未检测到 Calibre 或 Chromium 浏览器，将使用纯 Python 方案兜底"
            "（表格/公式支持有限，建议安装 Calibre 或 Edge/Chrome）。")

    def start_convert():
        if not state["files"]:
            messagebox.showwarning("提示", "请先选择 EPUB 文件或文件夹。")
            return
        out_dir = Path(out_var.get()) if out_var.get() else None
        convert_btn.config(state="disabled")

        def worker():
            total = len(state["files"])
            progress["maximum"] = total
            success = 0
            for i, f in enumerate(state["files"], 1):
                od = out_dir if out_dir else f.parent
                if convert_one(f, od, engine, log):
                    success += 1
                progress["value"] = i
            log("=" * 60)
            log(f"全部完成: 成功 {success}/{total}")
            convert_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()

    convert_btn.config(command=start_convert)
    root.mainloop()


# ============================== CLI ==============================

def run_cli(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="EPUB 转 PDF")
    parser.add_argument("input", help="EPUB 文件或包含 EPUB 的文件夹")
    parser.add_argument("-o", "--output", help="输出目录（默认与源文件同目录）",
                        default=None)
    args = parser.parse_args(argv)

    target = Path(args.input)
    if not target.exists():
        print(f"错误: 路径不存在: {target}")
        return 2

    files = collect_epub_files(target)
    if not files:
        print("未找到任何 EPUB 文件。")
        return 1

    engine = resolve_engine()
    if engine and engine.get("type") == "calibre":
        print(f"引擎: Calibre ({engine['exe']})")
    elif engine and engine.get("type") == "chromium":
        print(f"引擎: 无头浏览器 ({engine['name']})")
    else:
        print("引擎: 纯 Python 兜底（质量有限，建议安装 Calibre 或 Edge/Chrome）")
    out_dir = Path(args.output) if args.output else None
    success = 0
    for f in files:
        od = out_dir if out_dir else f.parent
        if convert_one(f, od, engine, print):
            success += 1
    print(f"\n全部完成: 成功 {success}/{len(files)}")
    return 0 if success == len(files) else 1


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
