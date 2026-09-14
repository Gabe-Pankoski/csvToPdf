# csvToPdf

Turns a question/answer CSV of employee records into a formatted PDF, optionally
keeping only the records whose author was present on that date according to a
second "presence" CSV.

## Usage

```
uv run python main.py <records.csv> [presence.csv] [output.pdf]
```

- With only `records.csv`, every record is written to the PDF.
- With `presence.csv`, a record is written only when its preparer is listed as
  present on the record's date in the presence table.
- `output.pdf` defaults to the records file name with a `.pdf` extension.

## Records CSV

Columns: `PreparedBy`, `Date`, `Section`, `Question`, `Answer`.

A record is one preparer on one date. Rows with the same `PreparedBy` and
`Date` are grouped into that record, and sections keep their order of first
appearance.

If the sheet has no `Date` column, all of a preparer's rows form one record and
its date is read from the row whose `Question` is `Date`. That row is shown in
the record heading rather than repeated as a question.

## Presence CSV

Either layout is accepted:

1. **Wide**: a `Date` column and an `Other Employees Present` column holding a
   list of names separated by commas, semicolons, pipes or newlines.
2. **Question/answer**: the same columns as the records CSV, with one row where
   `Question` is `Other Employees Present` and the date coming from a `Date`
   column or a row where `Question` is `Date`.

In both layouts the person named in `PreparedBy` also counts as present on that
date. Names are matched ignoring case and extra whitespace. Dates are matched
after normalising common formats (`2026-03-03`, `03/03/2026`, `March 3 2026`
and similar all compare equal).
