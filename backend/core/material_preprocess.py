"""素材预处理管线：把可成为学习材料的文件统一转成 Markdown。

主通道：微软 MarkItDown（PDF / DOCX / EPUB / PPTX / HTML / TXT / CSV / XLSX → Markdown）
OCR 兜底：RapidOCR（onnxruntime）—— 检测到扫描版 PDF（页面无文字层）时逐页 OCR
拆分：超长文档按标题结构拆成多份，主素材 + 补充素材
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 可处理为学习材料的扩展名
SUPPORTED_EXTS = {
    ".epub", ".pdf", ".docx", ".doc", ".txt", ".md", ".markdown",
    ".html", ".htm", ".pptx", ".xlsx", ".xls", ".csv",
}

# 单份素材最大字符数（约 2-3 万 token 的中文），超过则拆分
MAX_PART_CHARS = 60000

# 渲染 PDF 页面为图片的 DPI 缩放（OCR 用）
OCR_ZOOM = 2.0


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTS


def _convert_markitdown(path: Path) -> str:
    """MarkItDown 主转换通道。"""
    from markitdown import MarkItDown
    md = MarkItDown()
    result = md.convert(str(path))
    text = (result.text_content or "").strip()
    return text


def _html_to_markdown(soup) -> str:
    """把 BeautifulSoup 文档块级结构转为 Markdown。"""
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "pre", "blockquote", "table"]):
        if el.find_parent(["h1", "h2", "h3", "h4", "ul", "ol", "table"]):
            continue  # 只处理顶层块，避免重复
        name = el.name
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if name in ("h1", "h2", "h3", "h4"):
            lines.append(f"{'#' * int(name[1])} {text}")
        elif name == "li":
            lines.append(f"- {text}")
        elif name == "pre":
            lines.append(f"```\n{text}\n```")
        elif name == "blockquote":
            lines.append(f"> {text}")
        elif name == "table":
            rows = []
            for tr in el.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                lines.append(rows[0])
                lines.append("|" + "---|" * (rows[0].count("|") - 1))
                lines.extend(rows[1:])
        else:
            lines.append(text)
    return "\n\n".join(lines)


def _convert_epub(path: Path) -> str:
    """EPUB 专用解析：ebooklib 读章节 + BeautifulSoup 转 Markdown。

    MarkItDown 的 EPUB 支持不稳定（会退化成 zip 列表），这里独立实现。
    """
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(path))
    chapters = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        # 跳过 EPUB 自带导航页（nav/toc），只保留正文
        if item.get_name().lower().split("/")[-1] in ("nav.xhtml", "toc.xhtml", "nav.html", "toc.html"):
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        md = _html_to_markdown(soup)
        if md.strip():
            chapters.append(md)
    return "\n\n".join(chapters)


# ---------------------------------------------------------------------------
# OCR 兜底（扫描版 PDF / 图片）
# ---------------------------------------------------------------------------
_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def ocr_image_text(img) -> str:
    """对 PIL/numpy 图像做 OCR，返回文本。"""
    try:
        import numpy as np
        engine = _get_ocr()
        arr = np.array(img) if not isinstance(img, np.ndarray) else img
        result, _ = engine(arr)
        if not result:
            return ""
        return "\n".join(str(item[1]) for item in result)
    except Exception:
        return ""


def _pdf_has_text_layer(path: Path) -> bool:
    """判断 PDF 是否为扫描版（所有页均无文字层 → True）。"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        text_pages = 0
        for page in doc:
            if page.get_text().strip():
                text_pages += 1
        doc.close()
        return text_pages == 0
    except Exception:
        return False


def ocr_pdf(path: Path) -> str:
    """扫描版 PDF → 逐页 OCR → Markdown 文本。"""
    import fitz
    doc = fitz.open(str(path))
    chunks = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(OCR_ZOOM, OCR_ZOOM))
        img = pix.tobytes("png")
        from PIL import Image
        import io
        pil_img = Image.open(io.BytesIO(img))
        text = ocr_image_text(pil_img)
        if text.strip():
            chunks.append(f"<!-- 第 {i + 1} 页 -->\n{text}")
    doc.close()
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# 转换入口
# ---------------------------------------------------------------------------
def convert_to_markdown(path: Path) -> tuple[str, list[str]]:
    """把文件转为 Markdown 文本。

    返回 (md_text, warnings)
    warnings 记录降级/注意信息，如「扫描版 PDF，已用 OCR 识别」。
    """
    ext = path.suffix.lower()
    warnings: list[str] = []

    if ext == ".epub":
        try:
            text = _convert_epub(path)
            if text.strip():
                return text, warnings
        except Exception as e:
            warnings.append(f"EPUB 专用解析失败（{e}），退回 MarkItDown")
            try:
                text = _convert_markitdown(path)
                if text.strip():
                    return text, warnings
            except Exception as e2:
                raise ValueError(f"EPUB 解析失败：{e2}")

    if ext == ".pdf":
        if _pdf_has_text_layer(path):
            try:
                text = _convert_markitdown(path)
                if text:
                    return text, warnings
            except Exception as e:
                warnings.append(f"文本型 PDF 解析失败（{e}），尝试 OCR")
        else:
            warnings.append("检测到扫描版 PDF（无文字层），已使用 OCR 识别")
        try:
            text = ocr_pdf(path)
            return text, warnings
        except Exception as e:
            raise ValueError(f"扫描版 PDF OCR 失败：{e}")

    # 其余格式统一走 MarkItDown
    try:
        text = _convert_markitdown(path)
    except Exception as e:
        raise ValueError(f"{ext} 解析失败：{e}")
    if not text.strip():
        # txt 等纯文本兜底
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            text = ""
    return text, warnings


# ---------------------------------------------------------------------------
# 超长拆分
# ---------------------------------------------------------------------------
def _split_headings(text: str) -> list[str]:
    """按标题层级拆块，保证每块不超过 MAX_PART_CHARS。"""
    if len(text) <= MAX_PART_CHARS:
        return [text] if text.strip() else []

    # 优先按一级标题切
    blocks = re.split(r"(?m)^(?=# )", text)
    parts: list[str] = []
    buf = ""
    for b in blocks:
        if not b.strip():
            continue
        if len(b) > MAX_PART_CHARS:
            # 超长块再按二级标题切
            sub = re.split(r"(?m)^(?=## )", b)
            for s in sub:
                if not s.strip():
                    continue
                if len(s) > MAX_PART_CHARS:
                    # 仍未切动：按字符硬切（在段落边界处）
                    start = 0
                    while start < len(s):
                        end = min(start + MAX_PART_CHARS, len(s))
                        cut = s.rfind("\n\n", start + MAX_PART_CHARS // 2, end)
                        if cut == -1:
                            cut = end
                        parts.append(s[start:cut].strip())
                        start = cut
                else:
                    if buf and len(buf) + len(s) <= MAX_PART_CHARS:
                        buf += s
                    else:
                        if buf:
                            parts.append(buf.strip())
                        buf = s
        else:
            if buf and len(buf) + len(b) <= MAX_PART_CHARS:
                buf += b
            else:
                if buf:
                    parts.append(buf.strip())
                buf = b
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p.strip()]


def split_to_parts(text: str, base_name: str) -> list[dict]:
    """把 Markdown 拆成素材列表。

    base_name: 原始文件名（不含扩展名），如 计算机网络
    返回 [{name, content, role}]，role ∈ main / supplementary
    单份时 role=main；多份时第一份 main，其余 supplementary。
    """
    text = text.strip()
    if not text:
        return []
    blocks = _split_headings(text)
    if len(blocks) == 1:
        return [{"name": f"{base_name}.md", "content": blocks[0], "role": "main"}]
    parts = []
    for i, blk in enumerate(blocks):
        role = "main" if i == 0 else "supplementary"
        if i == 0:
            name = f"{base_name}.md"
        else:
            name = f"{base_name}_part{i + 1}.md"
        parts.append({"name": name, "content": blk, "role": role})
    return parts
