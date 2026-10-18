#!/usr/bin/env python3
"""Convert Marker JSON output to clean Markdown for Cambridge-style exercises.

Usage:
    python json_to_md.py path/to/marker.json -o out/section.md

Outputs:
    out/section.md              cleaned Markdown (figures saved to out/images/)
    out/section.review.txt      list of items that need manual / vision review

Requires: pip install beautifulsoup4 markdownify
"""
import argparse
import base64
import json
import re
import statistics
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md

MARK_RE = re.compile(r"^\s*\[\s*(\d+)\s*\]\s*$")
DASH_NUM_RE = re.compile(r"^\s*[—–-]\s*(\d{1,2})\s*[—–-]\s*$")
TRAIL_MARK_RE = re.compile(r"\s*\*{0,2}\[(\d+)\]\*{0,2}\s*$")
BAD_MARK_RE = re.compile(r"\[\d+\s*$")
OCR_MARK_FIX_RE = re.compile(r"\[(\d)[1lI|]\s*$")  # "[61" -> "[6]"
FOLIO_RE = re.compile(r"^\d{1,3}$")
# A single label like a, b, c ... 12, or i/ii/iii (but not common axis names)
LABEL_RE = re.compile(r"^(?:[a-z]|[A-Z]|[ivxlcdmIVXLCDM]{1,4}|\d{1,2})$")
AXIS_VARS = set("xysnmt")
NUM_LIKE_RE = re.compile(r"^[\d.,\-/% ]+$")
SKIP = {"PageHeader", "PageFooter"}

flags = []


# --------------------------------------------------------------------------
# HTML -> Markdown helpers
# --------------------------------------------------------------------------
def _flatten_lists(soup):
    """Turn <ol>/<ul>/<li> markup used for pure layout into newline separated text."""
    for li in soup.find_all("li"):
        li.insert(0, soup.new_tag("br"))
    for tag in soup.find_all(["ul", "ol"]):
        tag.unwrap()
    for li in soup.find_all("li"):
        li.unwrap()


def inline_md(fragment: str, force_inline: bool = False) -> str:
    """HTML -> Markdown, protecting LaTeX from markdown escaping.

    force_inline renders every <math> as inline $...$ (used inside table cells
    and flattened table rows so that equations don't blast the layout).
    """
    soup = BeautifulSoup(fragment, "html.parser")
    for img in soup.find_all("img"):
        if not img.get("src"):
            img.decompose()  # empty decorative icons
    maths = []
    for m in soup.find_all("math"):
        tex = m.get_text().strip()
        block = m.get("display") == "block"
        block = block and not force_inline
        maths.append((tex, block))
        m.replace_with(f" MATHTOKEN{len(maths) - 1}X ")
    text = md(str(soup), heading_style="ATX")
    for i, (tex, block) in enumerate(maths):
        rep = f"\n\n$${tex}$$\n\n" if block else f"${tex}$"
        pattern = rf"[ \t]*MATHTOKEN{i}X[ \t]*" if not block else rf"\s*MATHTOKEN{i}X\s*"
        text = re.sub(pattern, lambda _m, rep=rep, block=block: rep if block else f" {rep} ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def block_md(html: str, force_inline: bool = False) -> str:
    """inline_md() after linearising layout <ol>/<li> lists."""
    soup = BeautifulSoup(html, "html.parser")
    _flatten_lists(soup)
    return inline_md(str(soup), force_inline=force_inline)


def fmt_marks(text: str) -> str:
    """Normalise a trailing [n] to **[n marks]**, fixing/flagging OCR slips."""
    if "marks]" in text:
        return text
    fixed = OCR_MARK_FIX_RE.sub(r"[\1]", text)
    if fixed != text:
        flags.append(f"auto-fixed OCR mark: {text[-20:]!r} -> {fixed[-20:]!r}")
        text = fixed
    elif BAD_MARK_RE.search(text):
        flags.append("malformed mark allocation: " + text[-60:])
        return text + " <!-- CHECK: mark looks malformed -->"
    return TRAIL_MARK_RE.sub(lambda m: f" **[{m.group(1)} marks]**", text)


# --------------------------------------------------------------------------
# Table handling.  Marker heavily over-uses <table> for layout:
#   * exam questions with a/b/c sub-parts          -> flatten to text lines
#   * single-cell paragraphs                        -> plain paragraph
#   * equations laid out in a table                 -> flatten to text lines
#   * genuine multi-row data grids (x / values)     -> keep as Markdown table
# --------------------------------------------------------------------------
def question_rows(html: str):
    """Return [(num, body_html, marks)] if every row is question+marks, else None."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        texts = [c.get_text(" ", strip=True) for c in cells]
        if not any(texts):
            continue  # empty / duplicate rows
        if len(cells) < 2 or not MARK_RE.match(texts[-1]):
            return None  # genuine table: leave alone
        marks = MARK_RE.match(texts[-1]).group(1)
        cells, texts = cells[:-1], texts[:-1]
        num = None
        if texts and re.fullmatch(r"\d{1,2}", texts[0]):
            num = texts[0]
            cells = cells[1:]
        body = "".join(c.decode_contents() for c in cells).strip()
        if not body:
            return None
        rows.append((num, body, marks))
    return rows or None


def _table_rows(html: str):
    """Return list of rows, each a list of <td>/<th> elements."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if cells:
            rows.append(cells)
    return rows or None


def _cell_text(cell) -> str:
    t = cell.get_text(" ", strip=True).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t).strip()


def _is_label(text: str) -> bool:
    if not text or text in AXIS_VARS:
        return False
    return bool(LABEL_RE.fullmatch(text))


def _is_label_row(cells) -> bool:
    """True if the row is 'label  content' (e.g. 'a  $10^2=100$')."""
    ne = [_cell_text(c) for c in cells]
    ne = [t for t in ne if t]
    if len(ne) < 2:
        return False
    if not _is_label(ne[0]):
        return False
    if NUM_LIKE_RE.fullmatch(ne[1]):
        return False  # 'x | 5 | 10 | ...' is a data grid, not a label list
    return True


def is_data_table(rows) -> bool:
    """A table worth keeping as a Markdown grid."""
    content_rows = [r for r in rows if any(_cell_text(c) for c in r)]
    ne_counts = [sum(1 for c in r if _cell_text(c)) for r in content_rows]
    if len(ne_counts) < 2 or max(ne_counts) < 2:
        return False
    total = sum(len(r) for r in content_rows)
    empty = sum(1 for r in content_rows for c in r if not _cell_text(c))
    if total and empty / total > 0.35:
        return False
    try:
        most = statistics.mode(ne_counts)
    except statistics.StatisticsError:
        return False
    return all(abs(n - most) <= 1 for n in ne_counts)


def _clean_table_html(html: str) -> str:
    """Strip rows that have no content (OCR spacer rows) before building the table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return html
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if not any(_cell_text(c) for c in cells):
            tr.decompose()
    return str(table)


def _cell_inner(cell) -> str:
    """Inner HTML of a <td>/<th> (avoids markdownify adding a trailing '|')."""
    return "".join(str(x) for x in cell.contents)


def _join_cells_html(cells) -> str:
    return "".join(_cell_inner(c) for c in cells)


def render_table(html: str) -> list:
    """Render a block containing a <table>. Returns markdown lines."""
    lines = []
    rows = _table_rows(html)
    if not rows:
        return None

    # 1) question + [n marks] rows            "1 Some text [4]"
    q = question_rows(html)
    if q:
        for num, body, marks in q:
            text = inline_md(body)
            if num:
                text = f"**{num}** {text}"
            lines.append(f"{text} **[{marks} marks]**")
        return lines

    # 2) single-column "table"  -> one paragraph per row
    if all(sum(1 for c in r if _cell_text(c)) <= 1 for r in rows):
        for row in rows:
            ne = [c for c in row if _cell_text(c)]
            if ne:
                text = inline_md(_cell_inner(ne[0]), force_inline=True)
                if text:
                    lines.append(text)
        return lines

    # 3) label list  (a/b/c sub-parts, exercise question grids) -> text lines
    total = labels = 0
    for row in rows:
        if sum(1 for c in row if _cell_text(c)) >= 2:
            total += 1
            if _is_label_row(row):
                labels += 1
    if total >= 1 and labels / total >= 0.7:
        for row in rows:
            ne = [(i, c) for i, c in enumerate(row) if _cell_text(c)]
            if not ne:
                continue
            if len(ne) >= 2 and _is_label_row(row):
                rest = inline_md(_join_cells_html([c for _, c in ne[1:]]), force_inline=True)
                lines.append(f"**{_cell_text(ne[0][1])}** {rest}".rstrip())
            else:
                text = inline_md(_join_cells_html([c for _, c in ne]), force_inline=True)
                if text:
                    lines.append(text)
        return lines

    # 4) genuine data grid -> keep as Markdown table (inline maths only)
    if is_data_table(rows):
        text = inline_md(_clean_table_html(html), force_inline=True)
        if text:
            lines.append(text)
        return lines

    # 5) fallback: flatten the cells back into flowing lines
    for row in rows:
        ne = [c for c in row if _cell_text(c)]
        if ne:
            text = inline_md(_join_cells_html(ne), force_inline=True)
            if text:
                lines.append(text)
    return lines


# --------------------------------------------------------------------------
# Block renderer
# --------------------------------------------------------------------------
def render(block, page, out, img_dir, img_rel):
    bt = block["block_type"]
    if bt in SKIP:
        return
    html = block.get("html") or ""
    children = block.get("children")

    if bt in ("Page", "Document"):
        for ch in children or []:
            render(ch, page, out, img_dir, img_rel)
        return

    if bt == "ListGroup":
        items = []
        for ch in children or []:
            soup = BeautifulSoup(ch.get("html", ""), "html.parser")
            for t in soup.find_all(["ul", "p", "ol", "li"]):
                t.unwrap()
            text = inline_md(str(soup))
            text = re.sub(r"^[●•▪\s-]+", "", text)
            if text:
                items.append(f"- {text}")
        if items:
            out.append("\n".join(items))
        return

    if bt in ("Figure", "Diagram"):
        for key, b64 in (block.get("images") or {}).items():
            name = key.strip("/").replace("/", "_") + ".jpeg"
            img_dir.mkdir(parents=True, exist_ok=True)
            (img_dir / name).write_bytes(base64.b64decode(b64))
            out.append(f"![]({img_rel}/{name})")
        return

    # Decorative icons / cropped photo scraps: silently drop (flag in review)
    if bt == "Picture":
        if not html.strip():
            return
        bt = "Text"

    if bt == "SectionHeader":
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find(re.compile(r"h[1-6]"))
        level = int(tag.name[1]) if tag else 2
        text = soup.get_text(" ", strip=True)
        if text:
            out.append("#" * level + " " + text)
        return

    # Any block that contains a table is rendered through the table pipeline
    if "<table" in html:
        lines = render_table(html)
        if lines:
            out.append("\n".join(lines))
        return

    plain = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # Standalone page number sitting in the left gutter (15, 34, 42, ...)
    if bt == "Text" and FOLIO_RE.fullmatch(plain):
        x0, y0, x1, y1 = block["bbox"]
        if x0 < 45 and (x1 - x0) < 35:
            return

    # Empty block that covers a large region: OCR dropped real content
    if not html.strip():
        x0, y0, x1, y1 = block["bbox"]
        if (x1 - x0) > 100 and (y1 - y0) > 10:
            bbox = [round(v) for v in block["bbox"]]
            msg = f"page {page}: OCR returned nothing for region bbox {bbox}"
            flags.append(msg)
            out.append(f"<!-- CHECK: {msg} -->")
        return

    # Stray mark block like "[5]" -> attach to the previous chunk
    mm = MARK_RE.match(plain)
    if mm:
        if out and "marks]" not in out[-1] and not out[-1].startswith("!["):
            out[-1] = out[-1] + f" **[{mm.group(1)} marks]**"
        return

    # "— 15 —" is really question number 15
    m = DASH_NUM_RE.match(plain)
    if m:
        out.append(f"**{m.group(1)}**")
        return

    # Equation with no <math>: OCR failed, do not guess
    if bt == "Equation" and "<math" not in html:
        bbox = [round(v) for v in block["bbox"]]
        msg = f"page {page}: equation OCR failed, bbox {bbox}: {plain}"
        flags.append(msg)
        out.append(f"<!-- CHECK: {msg} -->")
        return

    text = block_md(html) if bt in ("Text", "ListItem") else inline_md(html)
    if text:
        out.append(fmt_marks(text))


# --------------------------------------------------------------------------
# Final text cleanup
# --------------------------------------------------------------------------
def _move_marks_before_citation(m):
    body, citation, marks = m.group(1), m.group(2), m.group(3)
    if "marks]" in body or body.startswith(("#", "!", "<!--", ">")):
        return m.group(0)  # not a plain question line, leave alone
    return f"{body} {marks}\n\n{citation}"


def final_cleanup(s: str) -> str:
    # drop-cap: "**i**   **G**iven" -> "**i** Given"
    s = re.sub(r"\*\*G\*\*iven", "Given", s)
    s = re.sub(r"(\*\*[ivx]+\*\*)\s{2,}", r"\1 ", s)
    # bold bare question numbers at line start: "9 Given" -> "**9** Given"
    s = re.sub(r"(?m)^(\d{1,2}) (?=[A-Z])", r"**\1** ", s)
    # marks that ended up after a citation line -> move onto the question text
    s = re.sub(
        r"(?m)^(.+?)\n\n(\*[^*\n]+\*) (\*\*\[\d+ marks\]\*\*)$",
        _move_marks_before_citation,
        s,
    )
    # marks glued to the end of a display equation -> own line
    s = re.sub(r"(\$\$[^\n]*\$\$)[ \t]*(\*\*\[\d+ marks\]\*\*)", r"\1\n\n\2", s)
    # stray empty headings
    s = re.sub(r"(?m)^#{1,6}\s*$", "", s)
    # a leftover expandable `<ol>` artifact lines
    s = re.sub(r"(?m)^\{[-+.] \}$", "", s)
    return s


def dedupe_consecutive(items):
    dedup = []
    for item in items:
        if dedup and item == dedup[-1]:
            continue
        dedup.append(item)
    return dedup


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    a = ap.parse_args()

    data = json.loads(a.json_file.read_text(encoding="utf-8"))
    dest = a.output or a.json_file.with_suffix(".clean.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img_dir = dest.parent / "images"

    out = []
    for i, page in enumerate(data["children"]):
        render(page, i + 1, out, img_dir, "images")

    text = final_cleanup("\n\n".join(dedupe_consecutive(out)).strip()) + "\n"
    dest.write_text(text, encoding="utf-8")

    review = dest.with_suffix(".review.txt")
    review.write_text("\n".join(flags) + ("\n" if flags else ""), encoding="utf-8")

    print(f"Wrote {dest}")
    print(f"{len(flags)} item(s) flagged (also saved to {review}):")
    for f in flags:
        print("  -", f)


if __name__ == "__main__":
    main()