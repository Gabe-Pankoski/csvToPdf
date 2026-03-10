import csv
import sys
from collections import OrderedDict
from pathlib import Path

from fpdf import FPDF


def clean(value: str) -> str:
    stripped = value.strip()
    return "N/A" if stripped == "" else stripped


Person = dict[str, list[tuple[str, str]]]  # {section: [(question, answer)]}


def parse_csv(input_path: Path) -> tuple[str, dict[str, Person]]:
    """Return (title, {name: {section: [(question, answer)]}})."""
    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return "", {}

    title = input_path.stem
    people: dict[str, Person] = OrderedDict()

    for row in rows:
        name = clean(row.get("PreparedBy", ""))
        section = clean(row.get("Section", ""))
        question = clean(row.get("Question", ""))
        answer = clean(row.get("Answer", ""))
        people.setdefault(name, OrderedDict()).setdefault(section, []).append((question, answer))

    return title, people


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


def csv_to_pdf(title: str, people: dict[str, Person], output_path: Path) -> None:
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

    for person_index, (name, sections) in enumerate(people.items()):
        # Divider between people (not before the first)
        if person_index > 0:
            pdf.ln(2)
            pdf.set_line_width(0.6)
            pdf.line(18, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(6)

        # ── Person heading ───────────────────────────────────────────
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(28, 8, "Prepared By:")
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 8, name, new_x="LMARGIN", new_y="NEXT")
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


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python main.py <input.csv> [output.pdf]")
        sys.exit(1)

    input_path = Path(sys.argv[1])

    if not input_path.exists():
        print(f"Error: file not found: {input_path}")
        sys.exit(1)

    if input_path.suffix.lower() != ".csv":
        print(f"Warning: expected a .csv file, got: {input_path.suffix}")

    output_path = (
        Path(sys.argv[2]) if len(sys.argv) >= 3 else input_path.with_suffix(".pdf")
    )

    title, people = parse_csv(input_path)

    if not people:
        print("CSV file is empty or missing expected columns.")
        sys.exit(1)

    csv_to_pdf(title, people, output_path)
    print(f"PDF written to: {output_path}")


if __name__ == "__main__":
    main()
