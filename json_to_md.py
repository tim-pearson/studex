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

# Optional LaTeX validity check used by the equation scoring (no hard dependency)
try:
    from latex2mathml.converter import convert as _latex_parse
except Exception:  # pragma: no cover - import not guaranteed
    _latex_parse = None

# --------------------------------------------------------------------------
# Header-level normalisation. Marker assigns heading levels inconsistently
# (TIP / WEB LINK / KEY POINT come out as both h1 and h4, EXERCISE as h2/h4),
# so we re-classify every SectionHeader by content against a stable scheme for
# Cambridge-style textbooks:
#   H1  chapter + chapter name
#   H2  numbered sections (2.1 ...), 'What is a logarithm?', checklist & review
#   H3  boxed callouts: KEY POINT, WORKED EXAMPLE, EXERCISE, TIP, WEB LINK,
#       DID YOU KNOW, FAST FORWARD, EXPLORE, PREREQUISITE KNOWLEDGE, Proofs,
#       plus 'checklist' subsections
#   H4  box internals: Answer, 'Multiplication law Division law ...'
#   H5  method/steps: 'a Method 1', 'Method 2', 'Step 3:', 'Use the power law.'
# --------------------------------------------------------------------------
HEADER_H2_RE = re.compile(
    r"^(?:"
    r"\d+\.\d+ |"                      # 2.1 Logarithms to base 10
    r"what is a logarithm|"            # chapter intro section
    r"checklist of learning and understanding|"
    r"end[- ]?of[- ]?chapter[- ]?review"
    r")",
    re.I,
)
HEADER_H3_RE = re.compile(
    r"^(?:"
    r"key point|"
    r"worked example|"
    r"exercise|"
    r"explore|"
    r"prerequisite knowledge|"
    r"web link|"
    r"fast forward|"
    r"tip\b|"
    r"did you know|"
    r"proofs|"
    r"the rules of logarithms|"
    r"natural logarithms|"
    r"transforming a relationship"
    r")",
    re.I,
)
HEADER_H4_RE = re.compile(
    r"^(?:"
    r"answer\b|"
    r"multiplication law division law"
    r")",
    re.I,
)
HEADER_H5_RE = re.compile(
    r"^(?:"
    r"a method\b|"
    r"method\s+\d|"
    r"step\s+\d|"
    r"use (?:the )?(?:power|division|equality|index|natural|index laws|rules)|"
    r"use equality of logarithms|"
    r"expand brackets|"
    r"collect |"
    r"divide both sides|"
    r"rearrange|"
    r"tak(?:e|ing) (?:natural |common )?log(?:arithms)?(?:s)? of|"
    r"write each side|"
    r"convert to exponential form|"
    r"factoris|"
    r"hence the graph"
    r")",
    re.I,
)
_last_header = ""


def classify_header(text: str) -> int:
    """Map a SectionHeader's text to a stable heading level (1-5)."""
    global _last_header
    t = text.strip()
    ts = re.sub(r"[*_`]", "", t).lower().strip()

    if re.match(r"^chapter\s+\d+", ts):
        _last_header = "chapter"
        return 1
    if _last_header == "chapter":  # chapter name right after 'Chapter N'
        _last_header = "name"
        return 1
    _last_header = "other"

    matches = HEADER_H5_RE.match(ts)
    if matches:
        return 5
    if HEADER_H4_RE.match(ts):
        return 4
    if HEADER_H3_RE.match(ts):
        return 3
    if HEADER_H2_RE.match(ts):
        return 2
    return 4  # unrecognised note/callout


RE_HEADING_LINE = re.compile(r"^(#+)\s+(.+?)\s*$", re.M)


def relevel_headings(text: str) -> str:
    """Re-classify any markdown heading lines that markdownify produced from
    inner <h1>-<h6> tags (e.g. '<h3>Answer</h3>' inside a Text block)."""
    return RE_HEADING_LINE.sub(
        lambda m: "#" * classify_header(m.group(2)) + " " + m.group(2), text
    )

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
def render(block, page, out, img_dir, img_rel, specials=None, overrides=None):
    bt = block["block_type"]
    if bt in SKIP:
        return
    html = block.get("html") or ""
    children = block.get("children")

    if bt in ("Page", "Document"):
        for ch in children or []:
            render(ch, page, out, img_dir, img_rel, specials, overrides)
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
        text = soup.get_text(" ", strip=True)
        if not text:
            return
        out.append("#" * classify_header(text) + " " + text)
        return

    # Equation blocks that are members of a merged cluster / dropped / replaced
    if bt == "Equation" and specials and id(block) in specials:
        kind, text = specials[id(block)]
        if kind == "emit":
            if text:
                out.append(text)
        elif kind == "replace":
            if text:
                out.append(text)
        # 'skip' / 'drop' -> nothing
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
        lbl = _first_label(html)
        if overrides and lbl and lbl in overrides.get(page, {}).get("labels", {}):
            out.append(f"**{lbl}** ${overrides[page]['labels'][lbl]}$")
            applied_ov.append((page, lbl, f"singleton label '{lbl}' replaced by override"))
            used_label_ov.add((page, lbl))
            return
        msg = f"page {page}: equation OCR failed, bbox {bbox}: {plain}"
        flags.append(msg)
        out.append(f"<!-- CHECK: {msg} -->")
        return

    if bt == "Equation":
        lbl = _first_label(html)
        if overrides and lbl and lbl in overrides.get(page, {}).get("labels", {}):
            out.append(f"**{lbl}** ${overrides[page]['labels'][lbl]}$")
            applied_ov.append((page, lbl, f"label '{lbl}' replaced by override"))
            used_label_ov.add((page, lbl))
            return
        # marker sometimes wraps an equation in <h2>; keep it as an equation
        soup = BeautifulSoup(html, "html.parser")
        for h in soup.find_all(re.compile(r"h[1-6]")):
            h.unwrap()
        text = inline_md(str(soup))
    else:
        text = block_md(html) if bt in ("Text", "ListItem") else inline_md(html)
    if text:
        out.append(fmt_marks(relevel_headings(text)))


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
# Equation de-duplication / "one equation per part".
#
# Marker's OCR emits several *overlapping* <p block-type="Equation"> blocks for
# a single dense multi-part row: one narrow tile per part ("a 5^x = 18") plus a
# wider block that re-reads (and often garbles) the left part while carrying the
# next "label + equation" ("x^* = 18 b 2^x = 35").  We cluster blocks on the
# same visual line, collect every candidate read for each label, score them and
# emit one equation per part.  Blocks that do not overlap are left untouched.
#
# Reports are keyed by the JSON page index (0-based) and the 1-based page number
# used everywhere else in this file, so they can be mapped back to the source.
# --------------------------------------------------------------------------
eq_checks = []       # (page_no, label, kind, current_text)
applied_ov = []      # (page_no, key, detail)
stale_ov = []        # (page_no, key, detail)
used_label_ov = set()  # (page_no, label) overrides actually consumed


def _plain(html: str) -> str:
    return re.sub(
        r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    ).strip()


def _eq_tokens(html: str):
    """Return ordered ('label', l) / ('math', tex) tokens from an Equation block.

    Labels come from <b>/<strong> anywhere, or from a bare single letter that
    appears either at the start of the block or directly after a <math>.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.p if soup.p is not None else soup
    toks = []
    single = re.compile(r"^[a-z]$", re.I)

    def bare(w):
        t = re.sub(r"[.,;:()'\"]", "", w)
        return t if single.fullmatch(t) else None

    def walk(el):
        for child in getattr(el, "children", []):
            nm = getattr(child, "name", None)
            if nm == "math":
                toks.append(("math", (child.get_text(" ", strip=True) or "").strip()))
            elif nm in ("b", "strong"):
                t = re.sub(r"\s+", "", child.get_text(" ", strip=True))
                if single.fullmatch(t):
                    toks.append(("label", t.lower()))
            elif nm in ("i", "em", "sub", "sup", "span"):
                continue
            elif isinstance(child, str):
                for w in child.replace("\xa0", " ").split():
                    t = bare(w)
                    if t is None:
                        continue
                    last = toks[-1] if toks else None
                    if last is None or last[0] == "math":
                        toks.append(("label", t.lower()))
            else:
                walk(child)

    walk(root)
    return toks


def _first_label(html: str):
    for k, v in _eq_tokens(html):
        if k == "label":
            return v
    return None


def _eq_score(tex: str, block_x1=None, anchor_x1=None) -> float:
    """Score a candidate equation read. Higher = more plausible."""
    s = 0.0
    if tex.count("{") == tex.count("}"):
        s += 2.0
    if tex.count("(") == tex.count(")"):
        s += 1.0
    if not re.search(r"[+\-^_=]\s*$", tex):
        s += 2.0
    s += min(len(tex), 40) / 40.0

    # suspicious tokens in garbled reads
    for bad in ("*", r"\ast", r"\dagger"):
        s -= 2.0 * tex.count(bad)
    if tex.count("|") % 2:  # unpaired pipe (stray |)
        s -= 2.0
    s -= 2.0 * len(re.findall(r"\bx\^[a-zA-Z]", tex))  # superscript on bare x
    s -= 2.0 * len(re.findall(r"[\^_]\{\s*-?\s*\}", tex))  # empty/dangling ^{} _{-}

    if block_x1 is not None and anchor_x1 is not None and abs(block_x1 - anchor_x1) <= 5:
        s += 1.0  # read ends where this label's tile is expected to end

    if _latex_parse is not None:
        try:
            _latex_parse(tex)
            s += 2.0
        except Exception:
            pass
    return s


def _expected_next(letter):
    if letter == "z":
        return None
    return chr(ord(letter) + 1)


def _split_glued_label(math: str, last_label):
    """Detect 'b = 3(16^x) - ...' where the label is glued into the math.

    Only splits when the letter is exactly the next label in sequence, is not
    x/y/z, and the token starts at the left edge of its tile (i.e. it is the
    first math with no preceding label). For the first label only accepts 'a'.
    """
    m = re.match(r"^([a-z])\s*=\s*(.+)$", math)
    if not m:
        return None
    letter, rest = m.group(1), m.group(2)
    if letter in "xyz":
        return None
    if last_label is None:
        if letter != "a":
            return None
    elif letter != _expected_next(last_label):
        return None
    return letter, rest


def _y_overlap_ratio(a, b):
    oy = min(a[3], b[3]) - max(a[1], b[1])
    if oy <= 0:
        return 0.0
    return oy / min(a[3] - a[1], b[3] - b[1])


def _table_eq(html):
    return "<table" in html


def _collect_page_equations(page):
    blocks = []

    def gather(node):
        h = node.get("html") or ""
        if node.get("block_type") == "Equation" and not _table_eq(h) and node.get("bbox"):
            blocks.append(node)
        for ch in node.get("children") or []:
            gather(ch)

    gather(page)
    return blocks


def _cluster_equations(blocks):
    """Union-find over overlapping Equation blocks on the same visual line."""
    n = len(blocks)
    if n < 2:
        return [None] * n
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        a, b = find(x), find(y)
        if a != b:
            parent[b] = a

    for i in range(n):
        for j in range(i + 1, n):
            A = blocks[i]["bbox"]
            B = blocks[j]["bbox"]
            if (
                _y_overlap_ratio(A, B) > 0.5
                and (min(A[2], B[2]) - max(A[0], B[0])) > 2.0
            ):
                union(i, j)
    return [find(k) for k in range(n)]


def _merge_cluster(members, page_no, labels_ov):
    """members: cluster Equation blocks in document order.

    Returns the merged markdown text for the row (one line per part)."""
    labels, seen, last_label = [], set(), None
    cands = {}         # label -> list[(tex, block)]
    anchor = {}        # label -> x1 of the tile that first read it
    direct = set()     # labels that have at least one direct (label,math) read
    inferred = {}      # label -> list[(tex, block)] from math-less label tiles
    orphans = []       # [(tex, block)] unlabelable  equations
    label_src = {}     # label -> garbled source text (for copy-paste CHECKs)
    pending = None

    for b in members:
        html = b.get("html") or ""
        bbox = b["bbox"]
        first_math = True
        saw_label = False
        pending = None  # a label without a following math must not leak to the next block
        toks = _eq_tokens(html)

        for k, v in toks:
            if k == "label":
                if v not in seen:
                    seen.add(v)
                    labels.append(v)
                    if "<math" not in html:
                        label_src.setdefault(v, _plain(html))
                last_label = v
                pending = v
                saw_label = True
                continue

            # ---- math token ----
            glued = None
            if first_math and not saw_label:
                glued = _split_glued_label(v, last_label)
                if glued:
                    reg, v = glued
                    if reg not in seen:
                        seen.add(reg)
                        labels.append(reg)
                    pending = reg
                    last_label = reg
            first_math = False

            if pending is not None:
                cands.setdefault(pending, []).append((v, b))
                anchor.setdefault(pending, bbox[2])
                direct.add(pending)
                pending = None
            elif last_label is not None:
                if last_label in direct:
                    cands.setdefault(last_label, []).append((v, b))
                else:
                    # a math-less tile (label with no direct read): the orphan
                    # that follows belongs to it -> mark as inferred, not shifted
                    inferred.setdefault(last_label, []).append((v, b))
            else:
                orphans.append((v, b))

    # mem -->
    parts = []
    for lbl in labels:
        if lbl in labels_ov:
            parts.append(f"**{lbl}** ${labels_ov[lbl]}$")
            applied_ov.append((page_no, lbl, f"label '{lbl}' replaced by override"))
            used_label_ov.add((page_no, lbl))
            continue

        src = cands.get(lbl) or []
        inf = inferred.get(lbl) or []
        if not src and not inf:
            eq_checks.append((page_no, lbl, "no_usable", label_src.get(lbl, "")))
            parts.append(
                f"<!-- CHECK: page {page_no}: no usable equation for part '{lbl}' -->"
            )
            continue

        def pick(cands_list, anchor_x1):
            scored = []
            for tex, blk in cands_list:
                scored.append(
                    (
                        _eq_score(tex, blk["bbox"][2], anchor_x1),
                        blk["bbox"][2] - blk["bbox"][0],
                        tex,
                    )
                )
            scored.sort(key=lambda t: (-t[0], t[1]))
            return scored[0], scored

        anchor_x1 = anchor.get(lbl)
        best, scored = pick(src if src else inf, anchor_x1)
        best_score, best_w, best_tex = best

        if src and not inf:
            # check for an ambiguous tie at the top score with different text
            rivals = [
                t for t in scored[1:] if abs(t[0] - best_score) < 1e-9 and t[2] != best_tex
            ]
            if rivals:
                eq_checks.append((page_no, lbl, "ambiguous", best_tex))
                parts.append(
                    f"**{lbl}** ${best_tex}$ <!-- CHECK: page {page_no}: ambiguous equation for part '{lbl}' -->"
                )
            else:
                parts.append(f"**{lbl}** ${best_tex}$")
        else:
            # best came from an inferred orphan after an OCR-failed label
            eq_checks.append((page_no, lbl, "inferred", best_tex))
            parts.append(
                f"**{lbl}** ${best_tex}$ <!-- CHECK: page {page_no}: equation for part '{lbl}' inferred from overlapping block -->"
            )

    seen_o = set()
    for tex, b in orphans:
        if tex in seen_o:
            continue
        seen_o.add(tex)
        parts.append(f"$${tex}$$")

    # keep review.txt parity: OCR-failed members still get a generic flag
    for b in members:
        if "<math" not in (b.get("html") or ""):
            bb = [round(v) for v in b["bbox"]]
            flags.append(
                f"page {page_no}: equation OCR failed, bbox {bb}: {_plain(b.get('html',''))}"
            )

    return "\n".join(parts) if parts else None


def build_specials(page, page_no, overrides, dedupe=True):
    """Pre-compute, per page, how clustered/dropped Equation blocks should render.

    Returns (specials, emitted_count) where specials maps block-id ->
    ('emit', text) | ('skip', None) | ('drop', None) | ('replace', text).
    """
    labels_ov = overrides.get(page_no, {}).get("labels", {})
    drop = overrides.get(page_no, {}).get("drop", [])
    replace = overrides.get(page_no, {}).get("replace", {})
    specials = {}

    blocks = _collect_page_equations(page)
    before = len(blocks)

    kept = []
    drop_used = set()
    for b in blocks:
        pt = _plain(b.get("html") or "")
        if pt in drop:
            idx = drop.index(pt)
            if idx in drop_used and pt != drop[idx]:
                idx = next((i for i, t in enumerate(drop) if t == pt and i not in drop_used), idx)
            drop_used.add(idx)
            specials[id(b)] = ("drop", None)
            applied_ov.append((page_no, f"drop[{pt!r}]", "dropped block"))
        else:
            kept.append(b)
    for i, txt in enumerate(drop):
        if i not in drop_used:
            stale_ov.append((page_no, f"drop_block_text[{i}]", f"no block matched {txt!r}"))

    emitted = 0
    clustered_members = set()
    if dedupe:
        owners = _cluster_equations(kept)
        clusters = {}
        for b, owner in zip(kept, owners):
            if owner is not None:
                clusters.setdefault(owner, []).append(b)
        for owner, members in clusters.items():
            if len(members) < 2:
                continue
            out_text = _merge_cluster(members, page_no, labels_ov)
            for t, mb in enumerate(members):
                clustered_members.add(id(mb))
                if t == 0:
                    specials[id(mb)] = ("emit", out_text)
                else:
                    specials[id(mb)] = ("skip", None)
            if out_text:
                emitted += out_text.count("\n") + 1

    # replace_block_text: only for blocks not in a cluster
    for b in blocks:
        if id(b) in specials:
            continue
        pt = _plain(b.get("html") or "")
        if pt in replace:
            specials[id(b)] = ("replace", replace[pt])
            applied_ov.append((page_no, f"replace[{pt!r}]", replace[pt]))
            replace.pop(pt, None)
    for old, new in list(replace.items()):
        stale_ov.append((page_no, f"replace_block_text[{old!r}]", "no unclustered block matched"))

    # singleton equation blocks (kept, not clustered, not replaced/dropped)
    for b in kept:
        if id(b) not in specials:
            emitted += 1

    return (specials or None), (before, emitted)


def load_overrides(json_path: Path, explicit: Path = None):
    """Load fixes.json. Returns {page_no: {"labels":{}, "drop":[], "replace":{}}}."""
    if explicit is not None:
        if not explicit.exists():
            print(f"error: --overrides file not found: {explicit}")
            raise SystemExit(1)
        path = explicit
    else:
        path = json_path.with_suffix(".fixes.json")
        if not path.exists():
            return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: {path} is not valid JSON: {e}")
        raise SystemExit(1)
    overrides = {}
    for page_key, ops in raw.items():
        try:
            page_no = int(page_key)
        except (TypeError, ValueError):
            print(f"error: fixes.json page key not an integer: {page_key!r}")
            raise SystemExit(1)
        page_ov = {"labels": {}, "drop": [], "replace": {}}
        for op_key, val in ops.items():
            if op_key in ("drop_block_text", "drop"):
                if isinstance(val, list):
                    page_ov["drop"] = [str(x) for x in val]
                else:
                    print(f"error: fixes.json page {page_no} {op_key} must be a list")
                    raise SystemExit(1)
            elif op_key in ("replace_block_text", "replace"):
                if isinstance(val, dict):
                    page_ov["replace"] = {str(k): str(v) for k, v in val.items()}
                else:
                    print(f"error: fixes.json page {page_no} {op_key} must be an object")
                    raise SystemExit(1)
            elif re.fullmatch(r"[a-z]", str(op_key)):
                page_ov["labels"][str(op_key)] = str(val)
            else:
                print(f"error: fixes.json page {page_no}: unknown key {op_key!r}")
                raise SystemExit(1)
        overrides[page_no] = page_ov
    return overrides


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument(
        "--no-dedupe",
        action="store_true",
        help="disable equation duplicate/cluster merging; work from the JSON as-is",
    )
    ap.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="path to fixes.json; default <json-stem>.fixes.json next to the input",
    )
    a = ap.parse_args()

    data = json.loads(a.json_file.read_text(encoding="utf-8"))
    dest = a.output or a.json_file.with_suffix(".clean.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img_dir = dest.parent / "images"

    overrides = load_overrides(a.json_file, a.overrides)

    out = []
    stats = []
    for i, page in enumerate(data["children"]):
        page_no = i + 1
        specials, (before, emitted) = build_specials(
            page, page_no, overrides, dedupe=not a.no_dedupe
        )
        render(page, page_no, out, img_dir, "images", specials, overrides)
        stats.append((i, page_no, before, emitted))

    text = final_cleanup("\n\n".join(dedupe_consecutive(out)).strip()) + "\n"
    dest.write_text(text, encoding="utf-8")

    # ---- review file ----
    review_lines = list(flags)
    for page_no, lbl, kind, cur in eq_checks:
        review_lines.append(
            f"page {page_no}: {kind} for part '{lbl}': {cur if cur else ''}".rstrip()
        )
    for page_no, key, detail in stale_ov:
        review_lines.append(f"page {page_no}: STALE override {key}: {detail}")
    review = dest.with_suffix(".review.txt")
    review.write_text("\n".join(review_lines) + ("\n" if review_lines else ""), encoding="utf-8")

    # ---- console summary ----
    print(f"Wrote {dest}")

    print("\n--- per-page equation stats (json index (0-based) -> page no) ---")
    print(f"{'idx':>3} {'page':>4} {'blocks_before':>13} {'equations_emitted':>17}")
    for idx, page_no, before, emitted in stats:
        print(f"{idx:>3} {page_no:>4} {before:>13} {emitted:>17}")

    # ---- overrides applied / stale ----
    for page_no, key, detail in applied_ov:
        print(f"override applied: page {page_no} {key}: {detail}")
    for page_no, key, detail in stale_ov:
        print(f"STALE override: page {page_no} {key}: {detail}")
    for page_no, ops in overrides.items():
        for lbl in ops.get("labels", {}):
            if (page_no, lbl) not in used_label_ov:
                print(f"STALE override: page {page_no} label '{lbl}': part not found")

    # ---- remaining CHECK flags, copy-paste form for fixes.json ----
    if eq_checks:
        grouped = {}
        for page_no, lbl, kind, cur in eq_checks:
            grouped.setdefault(str(page_no), {})[lbl] = cur if cur else "<missing>"
        print("\n--- remaining CHECK flags -> fixes.json entries ---")
        print("# fix each by replacing the value with the correct equation, e.g.:")
        print(json.dumps(grouped, indent=2, ensure_ascii=False))

    print(f"\n{len(flags) + len(eq_checks)} item(s) flagged (saved to {review})")
    for f in flags:
        print("  -", f)
    for page_no, lbl, kind, cur in eq_checks:
        print(f"  - page {page_no}: {kind} '{lbl}': {cur if cur else ''}".rstrip())
    for page_no, key, detail in stale_ov:
        print(f"  - STALE override page {page_no}: {key}: {detail}")


if __name__ == "__main__":
    main()