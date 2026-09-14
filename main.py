"""Render FLRA submissions to a PDF, keeping only the ones whose author was
present at the toolbox talk on the same day.

Both input CSVs are exports with the columns::

    Form Date, Prepared By, Section Title, Question Name, Answer: Type - Tab Delimited

Rows arrive in no particular order; one submission is the set of rows sharing
a ``Form Date`` and ``Prepared By``. Multi-select answers are tab delimited.

Usage::

    python main.py <flra.csv> [toolbox.csv] [output.pdf]
"""

import csv
import re
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from fpdf import FPDF


# ── Text helpers ────────────────────────────────────────────────────────


def clean(value: str | None) -> str:
    stripped = (value or "").strip()
    return "N/A" if stripped == "" else stripped


def normalize_header(value: str | None) -> str:
    """Lower-case a column header and strip everything but letters/digits."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def normalize_name(value: str | None) -> str:
    """Collapse whitespace and casefold so names compare reliably."""
    return " ".join((value or "").split()).casefold()


DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)


def normalize_date(value: str | None) -> str:
    """Return an ISO date (YYYY-MM-DD) when the value parses, else the stripped text."""
    text = " ".join((value or "").split())
    if not text:
        return ""
    # Exports sometimes carry a time component ("2026-09-08 07:15"); drop it.
    head = text.split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", text) else text
    for candidate in (text, head):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return text


def split_list(value: str | None) -> list[str]:
    """Split a delimited answer (tabs first, else commas/semicolons/pipes/newlines)."""
    raw = value or ""
    parts = raw.split("\t") if "\t" in raw else re.split(r"[,;|\n]", raw)
    return [" ".join(part.split()) for part in parts if part.strip()]


def split_names(value: str | None) -> list[str]:
    return split_list(value)


def format_answer(value: str | None) -> str:
    """Turn a raw answer into display text.

    Tab-delimited multi-select answers are joined with commas on one line,
    multi-line answers keep their line breaks, and blanks become ``N/A``.
    """
    raw = value or ""
    if "\t" in raw:
        items = split_list(raw)
        return ", ".join(items) if items else "N/A"
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return "\n".join(lines) if lines else "N/A"


def pdf_safe(text: str) -> str:
    """The core Helvetica font only knows cp1252; swap anything else for '?'."""
    return text.encode("cp1252", errors="replace").decode("cp1252")


# ── CSV reading ─────────────────────────────────────────────────────────


def find_column(fieldnames: list[str] | None, *candidates: str) -> str | None:
    """Return the real header matching one of the candidates.

    Exact normalized matches win; otherwise a header that *starts with* a
    candidate is accepted, so ``Answer: Type - Tab Delimited`` matches
    ``Answer``.
    """
    wanted = [normalize_header(c) for c in candidates]
    headers = [(h, normalize_header(h)) for h in fieldnames or []]
    for header, norm in headers:
        if norm in wanted:
            return header
    for header, norm in headers:
        if any(norm.startswith(w) for w in wanted if w):
            return header
    return None


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


class Columns:
    """Resolve the export's column names once per file."""

    def __init__(self, fieldnames: list[str]):
        self.name = find_column(fieldnames, "Prepared By", "PreparedBy", "Name", "Employee")
        self.date = find_column(fieldnames, "Form Date", "Date")
        self.section = find_column(fieldnames, "Section Title", "Section")
        self.question = find_column(fieldnames, "Question Name", "Question")
        self.answer = find_column(fieldnames, "Answer")

    @property
    def usable(self) -> bool:
        return None not in (self.name, self.question, self.answer)


# ── Data model ──────────────────────────────────────────────────────────

Person = dict[str, list[tuple[str, str]]]  # {section: [(question, answer)]}
RecordKey = tuple[str, str]  # (date, name)
Records = dict[RecordKey, Person]  # {(date, name): {section: [(question, answer)]}}
Presence = dict[str, set[str]]  # {date: {normalized names present}}

DATE_QUESTION = "date"
PRESENCE_QUESTION = "other employees present"
PRESENCE_HEADERS = ("Other Employees Present", "Employees Present", "Present", "Names")

# Sections are rendered in this order when present; anything else follows in
# order of first appearance in the file.
SECTION_ORDER = (
    "Project/Job Details",
    "INFORMATION",
    "TASK(S) TO BE COMPLETED",
    "PPE REQUIRED",
    "RISK RATING BEFORE CONTROLS",
    "ACTIVITY & PHYSICAL HAZARDS",
    "WORKING AT HEIGHTS",
    "ENVIRONMENTAL HAZARDS",
    "ERGONOMIC HAZARDS",
    "FINAL RISK RATING SCORE",
    "DISCUSSION",
    "Sign Off",
)


def section_rank(section: str, seen: dict[str, int]) -> tuple[int, int]:
    known = [normalize_header(s) for s in SECTION_ORDER]
    norm = normalize_header(section)
    if norm in known:
        return (0, known.index(norm))
    return (1, seen.get(section, len(seen)))


def parse_records(input_path: Path) -> tuple[str, Records]:
    """Return (title, {(date, name): {section: [(question, answer)]}}).

    Rows are grouped into one record per (Form Date, Prepared By); if a person
    filed several forms that day their distinct answers are listed under the
    repeated question. Records are
    sorted by date then name, sections follow ``SECTION_ORDER``, and questions
    within a section keep the order in which they first appear in the file so
    every record lays out the same way.

    If the sheet has no date column, all of a person's rows form one record
    whose date comes from the row whose question is ``Date``.
    """
    fieldnames, rows = read_rows(input_path)
    title = input_path.stem
    if not rows:
        return title, {}

    cols = Columns(fieldnames)
    if not cols.usable:
        return title, {}

    dates_by_name: dict[str, str] = {}
    if cols.date is None:
        for row in rows:
            if normalize_header(row.get(cols.question)) == normalize_header(DATE_QUESTION):
                dates_by_name.setdefault(clean(row.get(cols.name)), normalize_date(row.get(cols.answer)))

    grouped: dict[RecordKey, dict[str, dict[str, list[str]]]] = {}
    section_seen: dict[str, int] = {}
    question_seen: dict[str, dict[str, int]] = {}

    for row in rows:
        name = clean(row.get(cols.name))
        question = clean(row.get(cols.question))
        answer = format_answer(row.get(cols.answer))
        section = clean(row.get(cols.section)) if cols.section else "N/A"

        if cols.date is not None:
            date = normalize_date(row.get(cols.date))
        else:
            date = dates_by_name.get(name, "")
            if normalize_header(question) == normalize_header(DATE_QUESTION):
                grouped.setdefault((date, name), {})
                continue

        section_seen.setdefault(section, len(section_seen))
        question_seen.setdefault(section, {}).setdefault(question, len(question_seen[section]))
        answers = grouped.setdefault((date, name), {}).setdefault(section, {}).setdefault(question, [])
        if answer not in answers:
            answers.append(answer)

    records: Records = OrderedDict()
    for key in sorted(grouped, key=lambda k: (k[0], normalize_name(k[1]))):
        sections = grouped[key]
        person: Person = OrderedDict()
        for section in sorted(sections, key=lambda s: section_rank(s, section_seen)):
            order = question_seen[section]
            # A question can carry several distinct answers when the same
            # person filed more than one form that day (the export has no
            # form id to split them); each answer becomes its own row.
            person[section] = [
                (question, answer)
                for question in sorted(sections[section], key=order.__getitem__)
                for answer in sections[section][question]
            ]
        records[key] = person

    return title, records


def parse_presence(presence_path: Path) -> Presence:
    """Return {date: {normalized names present on that date}}.

    Two layouts are accepted:

    * A wide table with a date column and an ``Other Employees Present``
      column holding a delimited list of names.
    * The question/answer export layout, where one row per form has
      ``Question Name`` = ``Other Employees Present`` and the date comes from
      ``Form Date`` (or a ``Question`` = ``Date`` row).

    In both layouts the ``Prepared By`` person counts as present as well.
    """
    fieldnames, rows = read_rows(presence_path)
    presence: Presence = {}

    cols = Columns(fieldnames)
    names_col = find_column(fieldnames, *PRESENCE_HEADERS)

    def mark(date: str, names: list[str]) -> None:
        if not date:
            return
        bucket = presence.setdefault(date, set())
        bucket.update(normalize_name(n) for n in names if n.strip())

    if names_col is not None:
        for row in rows:
            date = normalize_date(row.get(cols.date)) if cols.date else ""
            names = split_names(row.get(names_col))
            if cols.name:
                names.append(row.get(cols.name) or "")
            mark(date, names)
        return presence

    if cols.question is None or cols.answer is None:
        return presence

    entries: dict[tuple[str, str], dict[str, list[str]]] = OrderedDict()
    for row in rows:
        preparer = (row.get(cols.name) or "") if cols.name else ""
        date = normalize_date(row.get(cols.date)) if cols.date else ""
        entry = entries.setdefault((preparer, date), {"date": [], "names": []})
        question = normalize_header(row.get(cols.question))
        if question == normalize_header(DATE_QUESTION):
            entry["date"].append(normalize_date(row.get(cols.answer)))
        elif question == normalize_header(PRESENCE_QUESTION):
            entry["names"].extend(split_names(row.get(cols.answer)))

    for (preparer, date), entry in entries.items():
        entry_date = date or next((d for d in entry["date"] if d), "")
        names = list(entry["names"])
        if preparer.strip():
            names.append(preparer)
        mark(entry_date, names)

    return presence


def filter_records(records: Records, presence: Presence) -> tuple[Records, list[tuple[RecordKey, str]]]:
    """Split records into (kept, dropped-with-reason) using the presence table."""
    kept: Records = OrderedDict()
    dropped: list[tuple[RecordKey, str]] = []
    for (date, name), sections in records.items():
        if date not in presence:
            dropped.append(((date, name), "no toolbox talk on that date"))
        elif normalize_name(name) not in presence[date]:
            dropped.append(((date, name), "not listed as present"))
        else:
            kept[(date, name)] = sections
    return kept, dropped


# ── PDF rendering ───────────────────────────────────────────────────────

PAGE_LEFT = 18
PAGE_RIGHT = 192
INDENT = 8  # mm indent for sections/rows under each record


class FormPDF(FPDF):
    def __init__(self, title: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._title = title

    def footer(self) -> None:
        self.set_y(-14)
        self.set_line_width(0.2)
        self.line(PAGE_LEFT, self.get_y(), PAGE_RIGHT, self.get_y())
        self.set_font("Helvetica", size=7)
        self.cell(0, 6, pdf_safe(f"{self._title}  |  Page {self.page_no()}"), align="C")


QUESTION_WIDTH = 60  # mm, as in the original layout
ROW_HEIGHT = 6  # mm line height for question/answer rows


def draw_row(pdf: FPDF, question: str, answer: str) -> None:
    """Draw one bold question cell and its answer beside it, original style.

    Both cells wrap when their text is too long for the column, and the row
    moves to a new page as a whole if it would not fit on the current one.
    """
    x = PAGE_LEFT + INDENT
    answer_width = PAGE_RIGHT - x - QUESTION_WIDTH
    q_text = pdf_safe(f"  {question.rstrip().rstrip(':').rstrip()}:")
    a_text = pdf_safe(answer)

    pdf.set_font("Helvetica", style="B", size=9)
    q_height = pdf.multi_cell(QUESTION_WIDTH, ROW_HEIGHT, q_text, align="L", dry_run=True, output="HEIGHT")
    pdf.set_font("Helvetica", size=9)
    a_height = pdf.multi_cell(answer_width, ROW_HEIGHT, a_text, align="L", dry_run=True, output="HEIGHT")
    row_height = max(q_height, a_height)

    if pdf.will_page_break(row_height):
        pdf.add_page()

    pdf.set_x(x)
    top = pdf.get_y()
    pdf.set_font("Helvetica", style="B", size=9)
    pdf.multi_cell(QUESTION_WIDTH, ROW_HEIGHT, q_text, align="L", new_x="RIGHT", new_y="TOP")
    pdf.set_font("Helvetica", size=9)
    pdf.multi_cell(answer_width, ROW_HEIGHT, a_text, align="L", new_x="LMARGIN", new_y="TOP")
    pdf.set_y(top + row_height)


def csv_to_pdf(title: str, records: Records, output_path: Path) -> None:
    pdf = FormPDF(title, orientation="portrait", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_left_margin(PAGE_LEFT)
    pdf.set_right_margin(210 - PAGE_RIGHT)
    pdf.add_page()

    # ── Title ────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", style="B", size=16)
    pdf.cell(0, 10, pdf_safe(title), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.4)
    pdf.line(PAGE_LEFT, pdf.get_y(), PAGE_RIGHT, pdf.get_y())
    pdf.ln(6)

    for record_index, ((date, name), sections) in enumerate(records.items()):
        # Divider between records (not before the first)
        if record_index > 0:
            pdf.ln(2)
            pdf.set_line_width(0.6)
            pdf.line(PAGE_LEFT, pdf.get_y(), PAGE_RIGHT, pdf.get_y())
            pdf.ln(6)

        # ── Record heading ───────────────────────────────────────────
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(28, 8, "Prepared By:")
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 8, pdf_safe(name), new_x="LMARGIN", new_y="NEXT")
        if date:
            pdf.set_font("Helvetica", style="B", size=12)
            pdf.cell(28, 8, "Date:")
            pdf.set_font("Helvetica", size=12)
            pdf.cell(0, 8, pdf_safe(date), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # ── Sections (indented) ───────────────────────────────────────
        pdf.set_left_margin(PAGE_LEFT + INDENT)
        for section, qa_pairs in sections.items():
            pdf.set_x(PAGE_LEFT + INDENT)
            pdf.set_font("Helvetica", style="B", size=10)
            pdf.cell(0, 7, pdf_safe(section), new_x="LMARGIN", new_y="NEXT")
            pdf.set_line_width(0.2)
            pdf.line(PAGE_LEFT + INDENT, pdf.get_y(), PAGE_RIGHT, pdf.get_y())
            pdf.ln(2)

            for question, answer in qa_pairs:
                draw_row(pdf, question, answer)

            pdf.ln(3)

        pdf.set_left_margin(PAGE_LEFT)

    pdf.output(str(output_path))


# ── CLI ─────────────────────────────────────────────────────────────────

USAGE = "Usage: python main.py <flra.csv> [toolbox.csv] [output.pdf]"


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(1)

    input_path = Path(args[0])
    presence_path: Path | None = None
    output_path: Path | None = None

    for arg in args[1:]:
        path = Path(arg)
        if path.suffix.lower() == ".csv" and presence_path is None:
            presence_path = path
        elif output_path is None:
            output_path = path
        else:
            print(USAGE)
            sys.exit(1)

    for path in (input_path, presence_path):
        if path is not None and not path.exists():
            print(f"Error: file not found: {path}")
            sys.exit(1)

    if input_path.suffix.lower() != ".csv":
        print(f"Warning: expected a .csv file, got: {input_path.suffix}")

    if output_path is None:
        output_path = input_path.with_suffix(".pdf")

    title, records = parse_records(input_path)

    if not records:
        print("CSV file is empty or missing expected columns.")
        sys.exit(1)

    if presence_path is not None:
        presence = parse_presence(presence_path)
        if not presence:
            print(f"Warning: no 'Other Employees Present' entries found in {presence_path}.")
        total = len(records)
        records, dropped = filter_records(records, presence)
        print(f"Kept {len(records)} of {total} submissions present in {presence_path.name}.")
        for (date, name), reason in dropped:
            print(f"  dropped {date or '(no date)'}  {name}: {reason}")
        if not records:
            print("No submissions match the toolbox talks; no PDF written.")
            sys.exit(1)

    csv_to_pdf(title, records, output_path)
    print(f"PDF written to: {output_path}")


if __name__ == "__main__":
    main()
