# csvToPdf

Renders exported FLRA (Field Level Risk Assessment) submissions to a PDF,
keeping only the submissions whose author was present at the toolbox talk on
the same day.

## Usage

```
uv run python main.py <flra.csv> [toolbox.csv] [output.pdf]
```

- With only the FLRA export, every submission is written to the PDF.
- With the toolbox export as well, a submission is written only when its
  `Prepared By` name appears in that day's `Other Employees Present` list.
- `output.pdf` defaults to the FLRA file name with a `.pdf` extension.
- The command prints how many submissions were kept and lists each dropped one
  with the reason (no toolbox talk that day, or not listed as present).

Both files are the standard export with these columns:

```
Form Date, Prepared By, Section Title, Question Name, Answer: Type - Tab Delimited
```

Rows may arrive in any order. Multi-select answers are tab delimited.

## FLRA export

One submission is the set of rows sharing a `Form Date` and `Prepared By`.
The PDF is rendered from an HTML template with WeasyPrint. Each day gets its
own page with a banner, and every submission on that day is a bordered block
with the preparer and date in its header. Sections follow the form's order
(Project/Job Details, Tasks, PPE, risk ratings, hazards, final rating); any
section not in that list follows in order of first appearance. Questions within
a section keep the order they first appear in the file so every submission
lays out the same way.

Each section is a two-column table. Tab-delimited answers become a bullet
list, multi-line answers keep their line breaks, and a question with no answer
(or an answer with no question) is shown as an italic note row spanning both
columns.

The export carries no form id. If one person files two forms on the same day
they are merged into one submission and each question that was answered
differently is listed twice, once per answer.

## Toolbox export

The same column layout. For each toolbox talk the row whose `Question Name` is
`Other Employees Present` supplies the tab-delimited list of names, and the
`Form Date` supplies the date. The person in `Prepared By` counts as present
too.

A simpler wide layout is also accepted: a `Date` column and an
`Other Employees Present` column holding names separated by tabs, commas,
semicolons, pipes or newlines.

Names are matched ignoring case and surrounding whitespace. Dates are compared
after normalising common formats (`2026-09-08`, `09/08/2026`, `Sept 8 2026`
and similar all compare equal).
