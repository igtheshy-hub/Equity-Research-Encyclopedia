"""PDF 抽取：PyMuPDF 抽文字层，扫描件自动 OCR 兜底。

锚定原则：page_no 永远等于 PDF 物理页号（1-based）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)


@dataclass
class ExtractedPage:
    page_no: int            # 1-based
    text: str
    ocr_used: bool
    ocr_engine: Optional[str]
    char_count: int
    error: Optional[str] = None


@dataclass
class ExtractResult:
    report_path: Path
    sha256: str
    page_count: int
    pages: List[ExtractedPage]
    metadata: dict

    @property
    def has_ocr(self) -> bool:
        return any(p.ocr_used for p in self.pages)


def sha256_file(path: Path) -> str:
    """分块读大 PDF 不爆内存。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _try_import_fitz():
    try:
        import fitz  # type: ignore[import-not-found]
        return fitz
    except Exception as e:  # noqa: BLE001
        log.warning("未安装 pymupdf：%s", e)
        return None


def _render_page_pixmap(fitz_doc, page_idx: int, dpi: int = 150):
    page = fitz_doc.load_page(page_idx)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    return page.get_pixmap(matrix=mat)


def _ocr_with_paddle(images: Iterable):
    """OCR 兜底：paddleocr 在用户未装或 Py3.14 装不上时**不抛错**，
    而是返回空 list，由调用方把页面标记为 ocr_failed."""
    try:
        from paddleocr import PaddleOCR  # type: ignore

        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    except Exception as e:  # noqa: BLE001
        log.warning("PaddleOCR 不可用：%s", e)
        return [(None, None)] * len(list(images))

    out: list[tuple[str, float]] = []
    for img in images:
        try:
            res = ocr.ocr(img, cls=True)
        except Exception as e:  # noqa: BLE001
            log.warning("PaddleOCR 单页失败：%s", e)
            out.append(("", 0.0))
            continue
        if not res or not res[0]:
            out.append(("", 0.0))
            continue
        lines = []
        for line in res[0]:
            txt = line[1][0] if line and len(line) > 1 else ""
            if txt:
                lines.append(txt)
        out.append(("\n".join(lines), 1.0))
    return out


def extract_pdf(
    path: Path,
    *,
    text_layer_threshold: int = 30,
    page_text_max_chars: int = 20_000,
    enable_ocr: bool = True,
    ocr_engine: str = "paddleocr",
    ocr_lang: str = "ch",
) -> ExtractResult:
    """主入口：从 PDF → 逐页文本（必要时 OCR）。"""
    fitz = _try_import_fitz()
    pdf_path = Path(path)
    sha = sha256_file(pdf_path)

    if fitz is None:
        log.error("需要安装 pymupdf 才能解析 PDF")
        return ExtractResult(
            report_path=pdf_path,
            sha256=sha,
            page_count=0,
            pages=[],
            metadata={"error": "pymupdf_missing"},
        )

    doc = fitz.open(pdf_path)
    page_count = doc.page_count
    extracted: List[ExtractedPage] = []

    for idx in range(page_count):
        page_no = idx + 1
        try:
            page = doc.load_page(idx)
            text = page.get_text("text") or ""
        except Exception as e:  # noqa: BLE001
            extracted.append(
                ExtractedPage(page_no=page_no, text="", ocr_used=False,
                              ocr_engine=None, char_count=0, error=str(e))
            )
            continue

        text = _clean_text(text)[:page_text_max_chars]
        if len(text.strip()) >= text_layer_threshold:
            extracted.append(
                ExtractedPage(
                    page_no=page_no, text=text, ocr_used=False,
                    ocr_engine=None, char_count=len(text),
                )
            )
            continue

        # 文字层过少：尝试 OCR
        if not enable_ocr or ocr_engine != "paddleocr":
            extracted.append(
                ExtractedPage(
                    page_no=page_no, text=text, ocr_used=False,
                    ocr_engine=None, char_count=len(text),
                    error="ocr_disabled",
                )
            )
            continue

        try:
            pix = _render_page_pixmap(fitz, idx, dpi=150)
            from io import BytesIO
            from PIL import Image

            img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            results = _ocr_with_paddle([img])
            ocr_text, conf = (results[0] if results else ("", 0.0))
            ocr_text = _clean_text(ocr_text or "")[:page_text_max_chars]
            if ocr_text.strip():
                extracted.append(
                    ExtractedPage(
                        page_no=page_no, text=ocr_text, ocr_used=True,
                        ocr_engine="paddleocr", char_count=len(ocr_text),
                    )
                )
            else:
                extracted.append(
                    ExtractedPage(
                        page_no=page_no, text=text or "(扫描页 OCR 未识别出文字)",
                        ocr_used=True, ocr_engine="paddleocr",
                        char_count=0, error="ocr_empty",
                    )
                )
        except Exception as e:  # noqa: BLE001
            extracted.append(
                ExtractedPage(page_no=page_no, text=text, ocr_used=True,
                              ocr_engine="paddleocr", char_count=len(text),
                              error=f"ocr_failed:{e}")
            )

    doc.close()

    meta = _guess_metadata(pdf_path, extracted)
    return ExtractResult(
        report_path=pdf_path,
        sha256=sha,
        page_count=page_count,
        pages=extracted,
        metadata=meta,
    )


_WS_RE = re.compile(r"[ \t]+", flags=re.UNICODE)
# 把软换行（PDF 行末换行）合并，但保留段分隔
_SOFTNL_RE = re.compile(r"(?<![。；！？!?.])\n")


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS_RE.sub(" ", s)
    s = _SOFTNL_RE.sub("", s)
    return s.strip()


def _guess_metadata(path: Path, pages: list[ExtractedPage]) -> dict:
    """从首页文本启发式抓 broker / title / date。"""
    if not pages:
        return {}
    head = pages[0].text[:2000]
    meta: dict = {"broker": "", "title": "", "report_date": None}

    # 日期：YYYY-MM-DD / YYYY/MM/DD / YYYY年MM月DD日
    m = re.search(
        r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", head
    )
    if m:
        try:
            meta["report_date"] = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except Exception:  # noqa: BLE001
            pass

    # 标题：第一行非空
    for line in head.splitlines():
        line = line.strip()
        if 6 <= len(line) <= 60:
            meta["title"] = line
            break

    # 券商：开头第一个词往往是券商简称
    first = head.strip().splitlines()[0] if head.strip() else ""
    tokens = re.findall(r"[一-鿿]{2,8}", first)
    if tokens:
        meta["broker"] = tokens[0]

    return meta


def save_uploaded_pdf(content: bytes, original_filename: str, dest_dir: Path) -> Path:
    """把上传的 PDF 落到 dest_dir；同名文件加索引避免覆盖。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", original_filename).strip() or "report.pdf"
    target = dest_dir / safe_name
    if target.exists():
        stem, suf = target.stem, target.suffix
        i = 1
        while True:
            cand = dest_dir / f"{stem}_{i}{suf}"
            if not cand.exists():
                target = cand
                break
            i += 1
    target.write_bytes(content)
    return target
