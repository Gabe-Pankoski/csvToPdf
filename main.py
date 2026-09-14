import csv
import re
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from fpdf import FPDF


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
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def split_names(value: str | None) -> list[str]:
    """Split a free-text list of names on commas, semicolons, pipes or newlines."""
    parts = re.split(r"[,;|\n]", value or "")
    return [" ".join(part.split()) for part in parts if part.strip()]


def find_column(fieldnames: list[str] | None, *candidates: str) -> str | None:
    """Return the real header whose normalized form matches one of the candidates."""
    wanted = {normalize_header(c) for c in candidates}
    for header in fieldnames or []:
        if normalize_header(header) in wanted:
            return header
    return None


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


Person = dict[str, list[tuple[str, str]]]  # {section: [(question, answer)]}
RecordKey = tuple[str, str]  # (name, date)
Records = dict[RecordKey, Person]  # {(name, date): {section: [(question, answer)]}}
Presence = dict[str, set[str]]  # {date: {normalized names present}}

DATE_QUESTION = "date"
PRESENCE_QUESTION = "other employees present"
PRESENCE_HEADERS = ("Other Employees Present", "Employees Present", "Present", "Names")


def parse_records(input_path: Path) -> tuple[str, Records]:
    """Return (title, {(name, date): {section: [(question, answer)]}}).

    The date of a record comes from a ``Date`` column when the sheet has one.
    Otherwise it is taken from the row whose ``Question`` is ``Date`` for that
    person, and all of that person's rows form a single record.
    """
    fieldnames, rows = read_rows(input_path)
    if not rows:
        return "", {}

    title = input_path.stem
    name_col = find_column(fieldnames, "PreparedBy", "Prepared By", "Name", "Employee")
    section_col = find_column(fieldnames, "Section")
    question_col = find_column(fieldnames, "Question")
    answer_col = find_column(fieldnames, "Answer")
    date_col = find_column(fieldnames, "Date")

    if name_col is None or question_col is None or answer_col is None:
        return title, {}

    records: Records = OrderedDict()

    if date_col is None:
        # No Date column: each person's rows form one record and the date is
        # whatever their "Date" question row says (if any).
        dates_by_name: dict[str, str] = {}
        for row in rows:
            name = clean(row.get(name_col))
            if normalize_header(row.get(question_col)) == normalize_header(DATE_QUESTION):
                dates_by_name.setdefault(name, normalize_date(row.get(answer_col)))

    for row in rows:
        name = clean(row.get(name_col))
        question = clean(row.get(question_col))
        answer = clean(row.get(answer_col))
        section = clean(row.get(section_col)) if section_col else "N/A"

        if date_col is not None:
            date = normalize_date(row.get(date_col))
        else:
            date = dates_by_name.get(name, "")
            if normalize_header(question) == normalize_header(DATE_QUESTION):
                # Shown in the record heading instead of repeated as a row.
                records.setdefault((name, date), OrderedDict())
                continue

        records.setdefault((name, date), OrderedDict()).setdefault(section, []).append(
            (question, answer)
        )

    return title, records


def parse_presence(presence_path: Path) -> Presence:
    """Return {date: {normalized names present on that date}}.

    Two layouts are accepted:

    * A wide table with a ``Date`` column and an ``Other Employees Present``
      column holding a delimited list of names.
    * The same question/answer layout as the records sheet, where one row per
      entry has ``Question`` = ``Other Employees Present`` and the date comes
      from a ``Date`` column or a ``Question`` = ``Date`` row.

    In both layouts the person in the ``PreparedBy`` column (if any) counts as
    present on that date as well.
    """
    fieldnames, rows = read_rows(presence_path)
    presence: Presence = {}

    name_col = find_column(fieldnames, "PreparedBy", "Prepared By", "Name", "Employee")
    date_col = find_column(fieldnames, "Date")
    names_col = find_column(fieldnames, *PRESENCE_HEADERS)
    question_col = find_column(fieldnames, "Question")
    answer_col = find_column(fieldnames, "Answer")

    def mark(date: str, names: list[str]) -> None:
        if not date:
            return
        bucket = presence.setdefault(date, set())
        bucket.update(normalize_name(n) for n in names if n.strip())

    if names_col is not None:
        # Wide layout: one row per entry.
        for row in rows:
            date = normalize_date(row.get(date_col)) if date_col else ""
            names = split_names(row.get(names_col))
            if name_col:
                names.append(row.get(name_col) or "")
            mark(date, names)
        return presence

    if question_col is None or answer_col is None:
        return presence

    # Question/answer layout: group rows into entries keyed by preparer (and
    # date column when present), then read the date/presence questions.
    entries: dict[tuple[str, str], dict[str, list[str]]] = OrderedDict()
    for row in rows:
        preparer = (row.get(name_col) or "") if name_col else ""
        date = normalize_date(row.get(date_col)) if date_col else ""
        entry = entries.setdefault((preparer, date), {"date": [], "names": []})
        question = normalize_header(row.get(question_col))
        if question == normalize_header(DATE_QUESTION):
            entry["date"].append(normalize_date(row.get(answer_col)))
        elif question == normalize_header(PRESENCE_QUESTION):
            entry["names"].extend(split_names(row.get(answer_col)))

    for (preparer, date), entry in entries.items():
        entry_date = date or next((d for d in entry["date"] if d), "")
        names = list(entry["names"])
        if preparer.strip():
            names.append(preparer)
        mark(entry_date, names)

    return presence


def filter_records(records: Records, presence: Presence) -> Records:
    """Keep only records whose preparer is listed as present on the record's date."""
    kept: Records = OrderedDict()
    for (name, date), sections in records.items():
        if normalize_name(name) in presence.get(date, set()):
            kept[(name, date)] = sections
    return kept


class FormPDF(FPDF):
    def __init__(self, title: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._title = title

    def footer(self) -> None:
        self.set_y(-14)
        self.set_line_width(0.2)
        self.line(18, self.get_y(), 192, self.get_y())
        self.set_font("Helvetica", size=7)
        self.cell(0, 6, f"{self._title}  |  Page {self.page_no()}", align="C")


def csv_to_pdf(title: str, records: Records, output_path: Path) -> None:
    pdf = FormPDF(title, orientation="portrait", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_left_margin(18)
    pdf.set_right_margin(18)
    pdf.add_page()

    # ── Title ────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", style="B", size=16)
    pdf.cell(0, 10, title, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.4)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(6)

    INDENT = 8  # mm indent for sections/rows under each person

    for record_index, ((name, date), sections) in enumerate(records.items()):
        # Divider between records (not before the first)
        if record_index > 0:
            pdf.ln(2)
            pdf.set_line_width(0.6)
            pdf.line(18, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(6)

        # ── Record heading ───────────────────────────────────────────
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(28, 8, "Prepared By:")
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 8, name, new_x="LMARGIN", new_y="NEXT")
        if date:
            pdf.set_font("Helvetica", style="B", size=12)
            pdf.cell(28, 8, "Date:")
            pdf.set_font("Helvetica", size=12)
            pdf.cell(0, 8, date, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # ── Sections (indented) ───────────────────────────────────────
        pdf.set_left_margin(18 + INDENT)
        for section, qa_pairs in sections.items():
            pdf.set_x(18 + INDENT)
            pdf.set_font("Helvetica", style="B", size=10)
            pdf.cell(0, 7, section, new_x="LMARGIN", new_y="NEXT")
            pdf.set_line_width(0.2)
            pdf.line(18 + INDENT, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(2)

            for question, answer in qa_pairs:
                pdf.set_x(18 + INDENT)
                pdf.set_font("Helvetica", style="B", size=9)
                pdf.cell(60, 6, f"  {question}:")
                pdf.set_font("Helvetica", size=9)
                pdf.cell(0, 6, answer, new_x="LMARGIN", new_y="NEXT")

            pdf.ln(3)

        pdf.set_left_margin(18)

    pdf.output(str(output_path))


USAGE = "Usage: python main.py <records.csv> [presence.csv] [output.pdf]"


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
            print(f"Warning: no presence entries found in {presence_path}; nothing to output.")
        total = len(records)
        records = filter_records(records, presence)
        print(f"Kept {len(records)} of {total} records present in {presence_path.name}.")
        if not records:
            print("No records match the presence table; no PDF written.")
            sys.exit(1)

    csv_to_pdf(title, records, output_path)
    print(f"PDF written to: {output_path}")


if __name__ == "__main__":
    main()
