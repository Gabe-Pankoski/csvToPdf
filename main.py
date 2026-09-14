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
import html as html_lib
import re
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from weasyprint import HTML


# ── Text helpers ────────────────────────────────────────────────────────


def clean(value: str | None) -> str:
    return (value or "").strip()


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
    """Split a delimited list (tabs first, else commas/semicolons/pipes/newlines)."""
    raw = value or ""
    parts = raw.split("\t") if "\t" in raw else re.split(r"[,;|\n]", raw)
    return [" ".join(part.split()) for part in parts if part.strip()]


def split_names(value: str | None) -> list[str]:
    return split_list(value)


def parse_answers(raw: str | None) -> list[str]:
    """Split a tab-delimited answer into individual answers, dropping blanks."""
    if not raw or not raw.strip():
        return []
    return [p.strip() for p in raw.split("\t") if p.strip()]


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

QA = tuple[str, list[str]]  # (question, [answers])
Person = dict[str, list[QA]]  # {section: [(question, [answers])]}
RecordKey = tuple[str, str]  # (date, name)
Records = dict[RecordKey, Person]  # {(date, name): {section: [(question, [answers])]}}
DayData = dict[str, dict[str, Person]]  # {date: {name: {section: [...]}}}
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
    """Return (title, {(date, name): {section: [(question, [answers])]}}).

    Rows are grouped into one record per (Form Date, Prepared By); if a person
    filed several forms that day their distinct answers are listed under the
    repeated question. Records are sorted by date then name, sections follow
    ``SECTION_ORDER``, and questions within a section keep the order in which
    they first appear in the file so every record lays out the same way.

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

    grouped: dict[RecordKey, dict[str, dict[str, list[list[str]]]]] = {}
    section_seen: dict[str, int] = {}
    question_seen: dict[str, dict[str, int]] = {}

    for row in rows:
        name = clean(row.get(cols.name))
        question = clean(row.get(cols.question))
        answers = parse_answers(row.get(cols.answer))
        section = clean(row.get(cols.section)) if cols.section else ""

        if not name:
            continue

        if cols.date is not None:
            date = normalize_date(row.get(cols.date))
        else:
            date = dates_by_name.get(name, "")
            if normalize_header(question) == normalize_header(DATE_QUESTION):
                grouped.setdefault((date, name), {})
                continue

        section_seen.setdefault(section, len(section_seen))
        question_seen.setdefault(section, {}).setdefault(question, len(question_seen[section]))
        variants = grouped.setdefault((date, name), {}).setdefault(section, {}).setdefault(question, [])
        if answers not in variants:
            variants.append(answers)

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
                (question, answers)
                for question in sorted(sections[section], key=order.__getitem__)
                for answers in sections[section][question]
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


def group_by_day(records: Records) -> DayData:
    """Regroup sorted records as {date: {name: sections}} for rendering."""
    days: DayData = OrderedDict()
    for (date, name), sections in records.items():
        days.setdefault(date, OrderedDict())[name] = sections
    return days


# ── HTML rendering ──────────────────────────────────────────────────────


def e(text: str) -> str:
    """HTML-escape a string."""
    return html_lib.escape(str(text))


def e_multiline(text: str) -> str:
    """HTML-escape a string, keeping its line breaks."""
    return "<br>".join(e(line.strip()) for line in text.splitlines() if line.strip())


def build_html(title: str, days: DayData) -> str:
    parts: list[str] = []

    parts.append(f'<h1 class="doc-title">{e(title)}</h1>')

    for day_index, (date, people) in enumerate(days.items()):
        pb = ' style="page-break-before: always;"' if day_index > 0 else ""
        parts.append(f'<div class="day-section"{pb}>')
        parts.append(f'  <div class="day-header">{e(date)}</div>')

        for name, sections in people.items():
            parts.append('  <div class="person-block">')
            parts.append(
                f'    <div class="person-header">'
                f'<span class="person-label">Prepared By:</span>'
                f'<span class="person-name">{e(name)}</span>'
                f'<span class="person-date-label">Date:</span>'
                f'<span class="person-date">{e(date)}</span>'
                f'</div>'
            )

            for section, qa_pairs in sections.items():
                parts.append('    <div class="section-block">')
                parts.append(f'      <div class="section-header">{e(section)}</div>')
                parts.append('      <table class="qa-table">')

                for question, answers in qa_pairs:
                    if not question and not answers:
                        continue

                    if not question or not answers:
                        # Instructional/note row — spans both columns
                        text = e(question) if question else e(" | ".join(answers))
                        parts.append(
                            f'        <tr class="note-row">'
                            f'<td colspan="2"><em>{text}</em></td></tr>'
                        )
                        continue

                    if len(answers) == 1:
                        answer_html = e_multiline(answers[0])
                    else:
                        items = "".join(f"<li>{e_multiline(a)}</li>" for a in answers)
                        answer_html = f"<ul>{items}</ul>"

                    parts.append(
                        f'        <tr>'
                        f'<th>{e(question)}</th>'
                        f'<td>{answer_html}</td>'
                        f'</tr>'
                    )

                parts.append('      </table>')
                parts.append('    </div>')  # section-block

            parts.append('  </div>')  # person-block

        parts.append('</div>')  # day-section

    return "\n".join(parts)


CSS = """
@page {
    size: A4;
    margin: 16mm 16mm 22mm 16mm;
    @bottom-left {
        content: string(doc-title);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 7pt;
        color: #555;
        border-top: 0.5pt solid #bbb;
        padding-top: 4pt;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 7pt;
        color: #555;
        border-top: 0.5pt solid #bbb;
        padding-top: 4pt;
    }
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9pt;
    line-height: 1.45;
    color: #1a1a1a;
    background: #fff;
}

/* ── Document title ─────────────────────────────────── */
.doc-title {
    font-size: 18pt;
    font-weight: bold;
    text-align: center;
    color: #1e3a5f;
    padding: 10pt 0 8pt;
    border-bottom: 2pt solid #1e3a5f;
    margin-bottom: 14pt;
    string-set: doc-title content();
}

/* ── Day section ────────────────────────────────────── */
.day-section {
    margin-bottom: 10pt;
}

.day-header {
    background-color: #1e3a5f;
    color: #ffffff;
    font-size: 12pt;
    font-weight: bold;
    padding: 5pt 8pt;
    letter-spacing: 0.03em;
    margin-bottom: 8pt;
}

/* ── Person block ───────────────────────────────────── */
.person-block {
    margin: 0 0 10pt 6mm;
    border: 0.5pt solid #c8d0da;
    border-radius: 2pt;
}

.person-header {
    background-color: #e8edf3;
    border-bottom: 0.5pt solid #c8d0da;
    padding: 4pt 8pt;
    font-size: 10pt;
    display: flex;
    justify-content: space-between;
}

.person-label, .person-date-label {
    font-weight: bold;
    color: #1e3a5f;
    margin-right: 6pt;
}

.person-date-label {
    margin-left: 16pt;
}

.person-name, .person-date {
    color: #1a1a1a;
}

/* ── Section block ──────────────────────────────────── */
.section-block {
    margin: 6pt 6pt 6pt 6pt;
}

.section-header {
    font-size: 8.5pt;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #2e6da4;
    border-bottom: 1pt solid #2e6da4;
    padding-bottom: 2pt;
    margin-bottom: 3pt;
    page-break-after: avoid;
}

/* ── Q&A table ──────────────────────────────────────── */
.qa-table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 4pt;
    font-size: 8.5pt;
}

.qa-table th {
    width: 45%;
    background-color: #f4f6f9;
    font-weight: normal;
    color: #333;
    text-align: left;
    vertical-align: top;
    padding: 3pt 6pt;
    border: 0.5pt solid #d8dde4;
}

.qa-table td {
    background-color: #ffffff;
    color: #1a1a1a;
    vertical-align: top;
    padding: 3pt 6pt;
    border: 0.5pt solid #d8dde4;
}

.qa-table ul {
    padding-left: 12pt;
    margin: 0;
}

.qa-table li {
    margin-bottom: 1pt;
}

.note-row td {
    background-color: #fafbfc;
    color: #555;
    font-style: italic;
    font-size: 8pt;
    padding: 2pt 6pt;
    border: 0.5pt solid #d8dde4;
}
"""


def csv_to_pdf(title: str, days: DayData, output_path: Path) -> None:
    body = build_html(title, days)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""
    HTML(string=html).write_pdf(str(output_path))


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

    csv_to_pdf(title, group_by_day(records), output_path)
    print(f"PDF written to: {output_path}")


if __name__ == "__main__":
    main()
