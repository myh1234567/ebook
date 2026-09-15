"""KDP 格式化与标准文件包生成器。
支持依据 Amazon KDP 规范输出：
1. 01_English_Manuscript.docx (含书名页、Heading 1样式、分页符、美式英语首行缩进排版)
2. 02_Publishing_Copy.docx
3. 03_Publishing_Copy.txt
4. 04_Internal_Synopsis.docx
5. 08_Adaptation_Bible.md
6. 09_Image_Prompts.txt
7. 10_Quality_Check_Report.md
8. 05_Ebook_Cover.png (若未提供图片，自动排版生成典雅排版预览封面)
"""
import html
import uuid
import zipfile
from pathlib import Path
from typing import List, Dict, Optional
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image, ImageDraw, ImageFont


def set_doc_language(doc: docx.Document, lang_code: str = "en-US"):
    """为整个文档设置默认校对语言为美式英语 (en-US)。"""
    for style in doc.styles:
        if style.type == WD_STYLE_TYPE.PARAGRAPH:
            try:
                rPr = style.element.get_or_add_rPr()
                lang = OxmlElement('w:lang')
                lang.set(qn('w:val'), lang_code)
                rPr.append(lang)
            except Exception:
                pass


def format_manuscript_docx(
    title: str,
    author: str,
    chapters: List[Dict[str, str]],
    output_path: Path,
    subtitle: str = "",
) -> Path:
    """生成符合 Amazon KDP 母稿标准的 01_English_Manuscript.docx。
    
    chapters 格式: [{"title": "Chapter 1: The Crossing", "content": "Paragraph 1\n\nParagraph 2"}]
    """
    doc = docx.Document()
    set_doc_language(doc, "en-US")

    # 基础正文样式
    normal_style = doc.styles['Normal']
    normal_style.font.name = 'Georgia'
    normal_style.font.size = Pt(11)
    normal_style.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
    normal_style.paragraph_format.line_spacing = 1.2
    normal_style.paragraph_format.space_after = Pt(0)
    normal_style.paragraph_format.first_line_indent = Inches(0.25)

    # 1. 英文书名页 (Title Page)
    p_pre = doc.add_paragraph()
    p_pre.paragraph_format.space_before = Pt(72)
    p_pre.paragraph_format.first_line_indent = Inches(0)
    p_pre.alignment = WD_ALIGN_PARAGRAPH.CENTER

    p_title = doc.add_paragraph()
    p_title.paragraph_format.first_line_indent = Inches(0)
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_title = p_title.add_run(title.upper())
    run_title.font.name = 'Georgia'
    run_title.font.size = Pt(24)
    run_title.bold = True

    if subtitle:
        p_sub = doc.add_paragraph()
        p_sub.paragraph_format.first_line_indent = Inches(0)
        p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_sub.paragraph_format.space_after = Pt(36)
        run_sub = p_sub.add_run(subtitle)
        run_sub.font.name = 'Georgia'
        run_sub.font.size = Pt(14)
        run_sub.italic = True
    else:
        p_title.paragraph_format.space_after = Pt(48)

    p_author = doc.add_paragraph()
    p_author.paragraph_format.first_line_indent = Inches(0)
    p_author.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_by = p_author.add_run("By\n")
    run_by.font.size = Pt(11)
    run_author = p_author.add_run(author or "Unknown Author")
    run_author.font.size = Pt(14)
    run_author.bold = True

    # 分页到正文
    doc.add_page_break()

    # 2. 逐章写入
    for i, ch in enumerate(chapters, 1):
        ch_title = ch.get("title", f"Chapter {i}")
        ch_content = ch.get("content", "").strip()

        # 章节标题使用 Heading 1，确保 Kindle 目录自动识别
        h1 = doc.add_heading(level=1)
        h1.paragraph_format.first_line_indent = Inches(0)
        h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        h1.paragraph_format.space_before = Pt(48)
        h1.paragraph_format.space_after = Pt(24)
        run_h1 = h1.add_run(ch_title)
        run_h1.font.name = 'Georgia'
        run_h1.font.size = Pt(18)
        run_h1.bold = True
        run_h1.font.color.rgb = RGBColor(0x11, 0x11, 0x11)

        paragraphs = [p.strip() for p in ch_content.split("\n") if p.strip()]
        for p_idx, text in enumerate(paragraphs):
            # 检查场景分隔符
            if text in ("* * *", "***", "---", "###"):
                sep = doc.add_paragraph()
                sep.paragraph_format.first_line_indent = Inches(0)
                sep.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
                sep.paragraph_format.space_before = Pt(12)
                sep.paragraph_format.space_after = Pt(12)
                r_sep = sep.add_run("* * *")
                r_sep.font.name = 'Georgia'
                r_sep.font.size = Pt(12)
                continue

            p = doc.add_paragraph()
            # 章节第一段通常不缩进 (标准西文小说排版惯例)
            if p_idx == 0:
                p.paragraph_format.first_line_indent = Inches(0)
            else:
                p.paragraph_format.first_line_indent = Inches(0.25)
            
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.15
            run = p.add_run(text)
            run.font.name = 'Georgia'
            run.font.size = Pt(11)

        # 章节末尾分页
        if i < len(chapters):
            doc.add_page_break()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def format_publishing_copy_docx(
    title: str,
    subtitle: str,
    author: str,
    series: str,
    blurb_text: str,
    blurb_html: str,
    short_promo: str,
    tagline: str,
    keywords_list: List[str],
    categories: List[str],
    search_keywords_7: List[str],
    output_path: Path,
) -> Path:
    """生成 02_Publishing_Copy.docx (KDP 出版资料完整 Word 包)。"""
    doc = docx.Document()
    set_doc_language(doc, "en-US")

    h = doc.add_heading("Amazon KDP Publishing Metadata Package", level=1)
    h.paragraph_format.first_line_indent = Inches(0)

    def add_section(header: str, body: str):
        sec = doc.add_heading(header, level=2)
        sec.paragraph_format.first_line_indent = Inches(0)
        p = doc.add_paragraph(body)
        p.paragraph_format.first_line_indent = Inches(0)
        p.paragraph_format.space_after = Pt(12)

    meta_info = (
        f"Title: {title}\n"
        f"Subtitle: {subtitle or 'N/A'}\n"
        f"Author / Pen Name: {author or 'N/A'}\n"
        f"Series: {series or 'Standalone'}\n"
        f"Language: English (en-US)"
    )
    add_section("1. Book Metadata", meta_info)
    add_section("2. Tagline", f"\"{tagline}\"")
    add_section("3. Short Promo Pitch (80-120 words)", short_promo)
    add_section("4. Amazon Book Description (Plain Text)", blurb_text)
    add_section("5. Amazon Book Description (KDP HTML Ready)", blurb_html)

    # 6个故事关键词
    kw_str = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(keywords_list)])
    kw_line = "; ".join(keywords_list)
    kw_content = f"{kw_str}\n\nSingle-line format for easy copying:\n{kw_line}"
    add_section("6. Six Story Keywords", kw_content)

    # 分类与搜索关键词
    cat_str = "\n".join([f"- {c}" for c in categories])
    add_section("7. KDP Category Recommendations", cat_str)

    s7_str = "\n".join([f"Box {i+1}: {k}" for i, k in enumerate(search_keywords_7)])
    add_section("8. KDP 7-Box Search Keywords (Optimized)", s7_str)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def format_synopsis_docx(
    title: str,
    author: str,
    synopsis_text: str,
    output_path: Path
) -> Path:
    """生成 04_Internal_Synopsis.docx (含剧透的内部梗概)。"""
    doc = docx.Document()
    set_doc_language(doc, "en-US")

    h1 = doc.add_heading(f"Internal Synopsis: {title}", level=1)
    h1.paragraph_format.first_line_indent = Inches(0)

    p_warn = doc.add_paragraph("[CONFIDENTIAL — CONTAINS COMPLETE SPOILERS & ENDING]")
    p_warn.runs[0].bold = True
    p_warn.runs[0].font.color.rgb = RGBColor(0xB2, 0x22, 0x22)
    p_warn.paragraph_format.first_line_indent = Inches(0)
    p_warn.paragraph_format.space_after = Pt(18)

    for para in synopsis_text.split("\n"):
        if para.strip():
            p = doc.add_paragraph(para.strip())
            p.paragraph_format.first_line_indent = Inches(0.25)
            p.paragraph_format.space_after = Pt(6)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


EPUB_CSS = """\
@page { margin: 0; }
body { margin: 5%; font-family: Georgia, serif; line-height: 1.5; text-align: justify; }
h1 { font-size: 1.5em; text-align: center; margin: 2em 0 1.5em; page-break-before: always;
     font-weight: normal; letter-spacing: 0.05em; }
p { margin: 0; text-indent: 1.25em; }
p.first { text-indent: 0; }
p.sep { text-indent: 0; text-align: center; margin: 1.2em 0; }
.title-page { text-align: center; margin-top: 25%; }
.title-page h1 { page-break-before: avoid; font-size: 2.2em; margin-bottom: 0.3em; }
.title-page .sub { font-size: 1.1em; font-style: italic; margin-bottom: 3em; }
.title-page .by { font-size: 0.9em; letter-spacing: 0.2em; }
.title-page .author { font-size: 1.3em; }
"""


def _xhtml(title: str, body: str) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en-US" lang="en-US">\n'
            f'<head><title>{html.escape(title)}</title>'
            f'<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f'<body>\n{body}\n</body>\n</html>\n')


def format_manuscript_epub(
    title: str,
    author: str,
    chapters: List[Dict[str, str]],
    output_path: Path,
    subtitle: str = "",
    cover_image: Optional[Path] = None,
) -> Path:
    """输出 KDP 可直接上传的 EPUB 3。

    比 DOCX 强在排版由我们说了算，不用赌 KDP 的自动转换；目录、语言、封面都嵌在文件里。
    手写 zip 而不引第三方库：EPUB 说到底就是一个结构固定的压缩包。
    """
    book_id = f"urn:uuid:{uuid.uuid4()}"
    items, spine, nav_items = [], [], []

    # 书名页
    sub_html = f'<p class="sub">{html.escape(subtitle)}</p>' if subtitle else ""
    items.append(("title.xhtml", _xhtml(title, (
        f'<div class="title-page"><h1>{html.escape(title)}</h1>{sub_html}'
        f'<p class="by">BY</p><p class="author">{html.escape(author)}</p></div>'))))
    spine.append("title.xhtml")

    for idx, ch in enumerate(chapters, 1):
        ch_title = (ch.get("title") or f"Chapter {idx}").strip()
        paras = []
        for i, raw in enumerate(p.strip() for p in ch.get("content", "").split("\n")):
            if not raw:
                continue
            if raw in ("***", "* * *", "---"):
                paras.append('<p class="sep">* * *</p>')
            else:
                cls = ' class="first"' if not paras else ""
                paras.append(f"<p{cls}>{html.escape(raw)}</p>")
        name = f"ch{idx:04d}.xhtml"
        items.append((name, _xhtml(ch_title,
                                   f"<h1>{html.escape(ch_title)}</h1>\n" + "\n".join(paras))))
        spine.append(name)
        nav_items.append((name, ch_title))

    nav_links = "\n".join(f'<li><a href="{n}">{html.escape(t)}</a></li>' for n, t in nav_items)
    nav = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
           f'xml:lang="en-US" lang="en-US"><head><title>Contents</title></head><body>'
           f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n{nav_links}\n</ol></nav>'
           f'</body></html>\n')

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    meta_cover = ""
    if cover_image and Path(cover_image).exists():
        manifest.append('<item id="cover-image" href="cover.png" media-type="image/png" '
                        'properties="cover-image"/>')
        meta_cover = '<meta name="cover" content="cover-image"/>'
    for i, (name, _) in enumerate(items):
        manifest.append(f'<item id="x{i}" href="{name}" media-type="application/xhtml+xml"/>')
    # spine 的顺序就是 items 的顺序：书名页在前，之后按章号排
    spine_xml = "\n".join(f'<itemref idref="x{i}"/>' for i in range(len(items)))

    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n'
           f'<dc:title>{html.escape(title)}</dc:title>\n'
           f'<dc:creator>{html.escape(author)}</dc:creator>\n'
           f'<dc:language>en-US</dc:language>\n'
           f'<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>\n{meta_cover}\n'
           f'</metadata>\n<manifest>\n' + "\n".join(manifest) + '\n</manifest>\n'
           f'<spine>\n{spine_xml}\n</spine>\n</package>\n')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as z:
        # mimetype 必须是压缩包里第一个文件且不压缩，阅读器靠它认格式
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>',
                   zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", EPUB_CSS, zipfile.ZIP_DEFLATED)
        for name, content in items:
            z.writestr(f"OEBPS/{name}", content, zipfile.ZIP_DEFLATED)
        if cover_image and Path(cover_image).exists():
            z.write(cover_image, "OEBPS/cover.png", zipfile.ZIP_DEFLATED)
    return output_path


FONT_DIRS = ("/System/Library/Fonts/Supplemental", "/Library/Fonts", "/System/Library/Fonts")


def _font(name: str, size: int):
    """按名字找系统字体，找不到就退回 Pillow 自带的，别为了字体把整条流程弄崩。"""
    for d in FONT_DIRS:
        f = Path(d) / name
        if f.exists():
            try:
                return ImageFont.truetype(str(f), size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> List[str]:
    lines, cur = [], []
    for word in text.split():
        cur.append(word)
        if draw.textlength(" ".join(cur), font=font) > max_w and len(cur) > 1:
            cur.pop()
            lines.append(" ".join(cur))
            cur = [word]
    if cur:
        lines.append(" ".join(cur))
    return lines


def _scrim(img: Image.Image, top_stop: float, bottom_start: float,
           top_alpha: int = 205, bottom_alpha: int = 215) -> Image.Image:
    """上下压暗。AI 底图再好看，字压不住就是废封面。"""
    w, h = img.size
    col = Image.new("L", (1, h), 0)
    # 顶部和底部各留一段"全黑平台"，再往中间渐隐——只靠渐变的话，
    # 落在渐变尾巴上的副标题还是会被底图的高光吃掉
    top_flat, bottom_flat = top_stop * 0.45, bottom_start + (1 - bottom_start) * 0.55
    for y in range(h):
        t = y / h
        a = 0
        if t < top_flat:
            a = top_alpha
        elif t < top_stop:
            a = int(top_alpha * (1 - (t - top_flat) / (top_stop - top_flat)) ** 1.2)
        if t > bottom_flat:
            a = max(a, bottom_alpha)
        elif t > bottom_start:
            a = max(a, int(bottom_alpha * ((t - bottom_start) / (bottom_flat - bottom_start)) ** 1.2))
        col.putpixel((0, y), a)
    return Image.composite(Image.new("RGB", (w, h), (8, 10, 18)), img, col.resize((w, h)))


def create_cover_graphic(
    title: str,
    author: str,
    subtitle: str = "",
    tagline: str = "",
    output_path: Path = Path("05_Ebook_Cover.png"),
    width: int = 1600,
    height: int = 2560,
    background: Optional[Path] = None
) -> Path:
    """生成 Amazon KDP 规范尺寸 (1600x2560, 1:1.6) 的封面。

    给了 background 就把 AI 画的插画裁成封面比例当底图，上下压暗后再排字；
    没给就退回纯色 + 金框。文字一律用 Pillow 画，不交给图像模型——
    图像模型写书名十次有九次是乱码。
    """
    GOLD, CREAM, WHITE = (212, 175, 55), (245, 239, 230), (255, 255, 255)
    safe_w = int(width * 0.80)
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # --- 先排版再画：文字块有多高要先量出来，蒙版才知道该压到哪 ---
    size = 132
    while size > 54:
        f_title = _font("Georgia Bold.ttf", size)
        title_lines = _wrap(ruler, title.upper(), f_title, safe_w)
        if len(title_lines) <= 3 and all(
                ruler.textlength(l, font=f_title) <= safe_w for l in title_lines):
            break
        size -= 6
    f_tag = _font("Georgia Italic.ttf", 46)
    f_sub = _font("Georgia Italic.ttf", 54)
    tag_lines = _wrap(ruler, tagline, f_tag, safe_w)[:2] if tagline else []
    sub_lines = _wrap(ruler, subtitle, f_sub, safe_w)[:2] if subtitle else []

    y0 = int(height * 0.095)
    block_bottom = (y0 + len(tag_lines) * 58 + (46 if tag_lines else 0)
                    + len(title_lines) * int(size * 1.18) + 72 + len(sub_lines) * 66)

    img, has_art = None, False
    if background and Path(background).exists():
        try:
            art = Image.open(background).convert("RGB")
            scale = max(width / art.width, height / art.height)
            art = art.resize((round(art.width * scale), round(art.height * scale)), Image.LANCZOS)
            left, top = (art.width - width) // 2, (art.height - height) // 2
            img = _scrim(art.crop((left, top, left + width, top + height)),
                         min(0.66, block_bottom / height + 0.10), 0.74)
            has_art = True
        except Exception:
            img = None

    if img is None:
        img = Image.new("RGB", (width, height), (18, 24, 38))
        m = 80
        d0 = ImageDraw.Draw(img)
        d0.rectangle([(m, m), (width - m, height - m)], outline=GOLD, width=6)
        d0.rectangle([(m + 16, m + 16), (width - m - 16, height - m - 16)],
                     outline=(140, 115, 36), width=2)

    draw = ImageDraw.Draw(img)
    stroke = dict(stroke_width=5, stroke_fill=(0, 0, 0)) if has_art else {}
    thin = dict(stroke_width=3, stroke_fill=(0, 0, 0)) if has_art else {}

    y = y0
    for line in tag_lines:
        draw.text((width // 2, y), line, font=f_tag, fill=(226, 220, 210), anchor="mm", **thin)
        y += 58
    if tag_lines:
        y += 46

    for line in title_lines:
        draw.text((width // 2, y), line, font=f_title, fill=CREAM, anchor="mm", **stroke)
        y += int(size * 1.18)

    y += 10
    draw.line([(width // 2 - 190, y), (width // 2 + 190, y)], fill=GOLD, width=4)
    draw.ellipse([(width // 2 - 11, y - 11), (width // 2 + 11, y + 11)], fill=GOLD)
    y += 62

    # 有底图时副标题不能用金色：压在暖色高光上根本读不出来，换浅色加粗描边
    sub_fill = (242, 234, 220) if has_art else GOLD
    sub_stroke = dict(stroke_width=4, stroke_fill=(0, 0, 0)) if has_art else {}
    for line in sub_lines:
        draw.text((width // 2, y), line, font=f_sub, fill=sub_fill, anchor="mm", **sub_stroke)
        y += 66

    # 作者名固定在底部，字间距拉开一点更像出版物
    f_author = _font("Georgia.ttf", 66)
    name = " ".join((author or "ANONYMOUS").upper())
    if draw.textlength(name, font=f_author) > safe_w:
        name = (author or "ANONYMOUS").upper()
    draw.text((width // 2, int(height * 0.905)), name, font=f_author, fill=WHITE,
              anchor="mm", **stroke)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG")
    return output_path
