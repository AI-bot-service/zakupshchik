"""КП в DOCX по одностраничному шаблону заказчицы (istochniki/drive/shablon_kp.pdf).

Шапка, обращение, таблица и условия — как в шаблоне. Пустая цена — ячейка с заливкой.
Печати и подписи нет: их ставит человек после проверки; робот КП не отправляет.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.core.provenance import BUILD_MARK
from app.kp.draft import KpDraft

LOGO = Path(__file__).parent / "assets" / "logo.jpg"
FONT = "Calibri"
BLANK_FILL = "FFF2CC"
GRAY = RGBColor(0x59, 0x59, 0x59)
ADDRESS = (
    "119021, город Москва, ул Льва Толстого, д.\n"
    "5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)\n"
    "Т +7 495 232 00 23 | info@softline.com\n"
    "www.softline.ru | www.softline.com"
)
INTRO = "ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:"
SLOGAN = "We know we can\nDigital Transformation and Cybersecurity Solution Service Provider"
HEAD = ("Артикул", "Наименование", "Кол-во", "Цена, руб.", "Сумма, руб.", "В т.ч. НДС, руб.")
WIDTHS = (Cm(2.8), Cm(7.4), Cm(1.4), Cm(2.1), Cm(2.2), Cm(2.1))


def money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def qty_text(value: Decimal) -> str:
    return f"{value.normalize():f}".replace(".", ",")


def _text(
    par: Paragraph, text: str, size: float = 10.5, bold: bool = False, color: RGBColor | None = None
) -> None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        run = par.add_run(line)
        run.font.name = FONT
        run.font.size = Pt(size)
        run.bold = bold
        if color is not None:
            run.font.color.rgb = color
        if i < len(lines) - 1:
            run.add_break()


def _para(
    parent: DocxDocument | _Cell,
    text: str = "",
    align: WD_ALIGN_PARAGRAPH | None = None,
    size: float = 10.5,
    color: RGBColor | None = None,
) -> Paragraph:
    par = parent.add_paragraph()
    par.paragraph_format.space_after = Pt(0)
    if align is not None:
        par.alignment = align
    if text:
        _text(par, text, size=size, color=color)
    return par


def _fill(cell: _Cell, color: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shd)


def _cell(
    cell: _Cell, text: str, bold: bool = False, align: WD_ALIGN_PARAGRAPH = WD_ALIGN_PARAGRAPH.CENTER
) -> None:
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    par = cell.paragraphs[0]
    par.alignment = align
    _text(par, text, size=10, bold=bold)


def _no_borders(table: Table) -> None:
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    table._tbl.tblPr.append(borders)


def _widths(table: Table) -> None:
    """Ширины и в сетке таблицы, и в ячейках: LibreOffice смотрит на сетку, Word — на ячейки."""
    table.autofit = False
    for col, width in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), WIDTHS, strict=True):
        col.set(qn("w:w"), str(width.twips))
    for row in table.rows:
        for cell, width in zip(row.cells, WIDTHS, strict=True):
            cell.width = width


def render_docx(draft: KpDraft, path: Path) -> Path:
    doc = Document()
    section = doc.sections[0]
    section.page_height, section.page_width = Cm(29.7), Cm(21.0)
    section.left_margin = section.right_margin = Cm(1.5)
    section.top_margin, section.bottom_margin = Cm(1.5), Cm(1.5)

    head = doc.add_table(rows=1, cols=2)
    _no_borders(head)
    left, right = head.rows[0].cells
    left.width, right.width = Cm(10), Cm(8)
    left.paragraphs[0].add_run().add_picture(str(LOGO), width=Cm(5.2))
    _para(left, ADDRESS, size=10, color=GRAY)
    right.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _text(right.paragraphs[0], f"\nКуда: {draft.customer}")
    _para(right, f"\nИсх. № бн от {draft.issued:%d.%m.%Y}.", WD_ALIGN_PARAGRAPH.RIGHT)

    for _ in range(3):
        _para(doc)
    _para(doc, "Уважаемый заказчик!", WD_ALIGN_PARAGRAPH.CENTER)
    _para(doc)
    intro = _para(
        doc, "ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:"
    )
    intro.paragraph_format.first_line_indent = Cm(1)
    intro.paragraph_format.space_after = Pt(8)

    table = doc.add_table(rows=1, cols=len(HEAD))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, title in zip(table.rows[0].cells, HEAD, strict=True):
        _cell(cell, title, bold=True)
    for line in draft.lines:
        row = table.add_row().cells
        vat = line.vat(draft.vat_rate)
        values = (
            line.sku,
            line.name,
            qty_text(line.qty),
            money(line.unit_price),
            money(line.amount),
            "Без НДС" if line.vat_free else money(vat),
        )
        for i, (cell, value) in enumerate(zip(row, values, strict=True)):
            _cell(cell, value, align=WD_ALIGN_PARAGRAPH.LEFT if i == 1 else WD_ALIGN_PARAGRAPH.CENTER)
        if line.unit_price is None:
            for cell in row[3:5] if line.vat_free else row[3:6]:
                _fill(cell, BLANK_FILL)
    total = table.add_row().cells
    _cell(total[1], "Итого" if not draft.blank_prices else "Итого (без позиций с пустой ценой)", bold=True)
    priced = draft.blank_prices < len(draft.lines)
    _cell(total[4], money(draft.total) if priced else "", bold=True)
    vat_total = draft.vat_total
    all_free = all(ln.vat_free for ln in draft.lines)
    _cell(total[5], "Без НДС" if all_free else money(vat_total), bold=True)
    if not priced:
        _fill(total[4], BLANK_FILL)
    if not all_free and vat_total is None:
        _fill(total[5], BLANK_FILL)
    _widths(table)

    _para(doc)
    for note in (
        "Цена указана с учетом всех налогов, сборов, и других обязательных платежей.",
        f"Условия оплаты: {draft.payment}.",
        f"Срок поставки: {draft.delivery}.",
        f"Срок действия коммерческого предложения - до {draft.valid_until:%d.%m.%Y}.",
    ):
        _para(doc, note, size=10).paragraph_format.left_indent = Cm(1)

    for _ in range(4):
        _para(doc)
    _para(doc, f"{draft.signer_name}\n{draft.signer_title}", size=10).paragraph_format.left_indent = Cm(0.5)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _text(footer, SLOGAN, 9, color=GRAY)

    props = doc.core_properties
    props.title = f"Коммерческое предложение — {draft.customer}"[:250]
    props.author = "Закупщик (черновик для менеджера)"
    props.keywords = BUILD_MARK
    props.comments = (
        "Цены — по каталогу на дату формирования, подтверждает менеджер. Подпись и печать — вручную."
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
