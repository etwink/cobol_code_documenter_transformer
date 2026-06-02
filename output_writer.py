"""Write transformation pipeline output to disk.

Produces:
  <output_dir>/
    python_db/          ← one .py file per COBOL source (DB version)
    python_etl/         ← one .py file per COBOL source (ETL/file version)
    documentation.md    ← comprehensive technical documentation (Markdown)
    etl_operations.csv  ← flat inventory of all detected ETL operations

Usage:

    writer = OutputWriter(output_dir=Path("output"))
    writer.write(pipeline_output)
"""

import csv
import textwrap
from datetime import datetime
from pathlib import Path

from transformation_pipeline import TransformationOutput, TransformationDocument
from cobol_transformer import PythonTransformationResult
from etl_detector import ETLOperation


class OutputWriter:
    """Write a TransformationOutput to structured files on disk."""

    def __init__(self, output_dir: Path | str = Path("output")):
        self.output_dir = Path(output_dir)

    def write(self, output: TransformationOutput) -> Path:
        """Write all outputs and return the output directory path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "python_db").mkdir(exist_ok=True)
        (self.output_dir / "python_etl").mkdir(exist_ok=True)

        for result in output.transformations:
            self._write_python_file(result, "python_db",  result.python_db_code)
            self._write_python_file(result, "python_etl", result.python_etl_code)

        if output.all_etl_operations:
            self._write_etl_csv(output.all_etl_operations)

        if output.documentation:
            self._write_markdown(output.documentation, output.transformations)

        return self.output_dir

    # ── Python files ────────────────────────────────────────────────────────

    def _write_python_file(
        self, result: PythonTransformationResult, subdir: str, code: str
    ) -> None:
        stem = Path(result.source_file).stem.lower()
        dest = self.output_dir / subdir / f"{stem}.py"
        dest.write_text(code, encoding="utf-8")

    # ── ETL operations CSV ───────────────────────────────────────────────────

    def _write_etl_csv(self, ops: list[ETLOperation]) -> None:
        dest = self.output_dir / "etl_operations.csv"
        with dest.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "line_number", "operation_type", "table_or_file",
                "is_read", "description", "raw_statement",
            ])
            for op in ops:
                writer.writerow([
                    op.line_number,
                    op.operation_type.value,
                    op.table_or_file,
                    op.is_read,
                    op.description,
                    op.raw_statement[:120],
                ])

    # ── Markdown documentation ───────────────────────────────────────────────

    def _write_markdown(
        self,
        doc: TransformationDocument,
        transformations: list[PythonTransformationResult],
    ) -> None:
        dest = self.output_dir / "documentation.md"
        dest.write_text(
            _build_markdown(doc, transformations),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_markdown(
    doc: TransformationDocument,
    transformations: list[PythonTransformationResult],
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_list = "\n".join(
        f"- `{Path(t.source_file).name}` → "
        f"`python_db/{Path(t.source_file).stem.lower()}.py` / "
        f"`python_etl/{Path(t.source_file).stem.lower()}.py`"
        for t in transformations
    )

    # Build transformation notes table
    notes_rows = []
    for t in transformations:
        stem = Path(t.source_file).stem
        write_ops = [op for op in t.etl_operations if not op.is_read]
        read_ops  = [op for op in t.etl_operations if op.is_read]
        truncated = "Yes" if "Source truncated   : Yes" in t.transformation_notes else "No"
        notes_rows.append(
            f"| `{stem}` | {len(read_ops)} | {len(write_ops)} | {truncated} |"
        )
    notes_table = (
        "| Module | Read Ops | Write/Modify Ops | Source Truncated |\n"
        "|--------|----------|------------------|------------------|\n"
        + "\n".join(notes_rows)
    ) if notes_rows else "_No transformations performed._"

    # Build per-file assumptions
    assumptions_per_file = []
    for t in transformations:
        if t.assumptions:
            stem = Path(t.source_file).stem
            bullets = "\n".join(f"- {a}" for a in t.assumptions)
            assumptions_per_file.append(f"**{stem}**\n\n{bullets}")

    assumptions_detail = (
        "\n\n---\n\n".join(assumptions_per_file)
        if assumptions_per_file
        else "_No per-file assumptions recorded._"
    )

    sections = [
        _h1("COBOL to Python — Technical Transformation Document"),
        f"_Generated: {generated_at}_\n",

        _h2("Table of Contents"),
        _toc(),

        _h2("1. Overview"),
        doc.overview,

        _h2("2. Assumptions"),
        _callout(
            "The COBOL source may not fully reveal the beginning or end of the business process. "
            "The assumptions below represent best-guess inferences made during transformation. "
            "They should be validated with the original system owners."
        ),
        doc.assumptions,
        _h3("2.1 Per-File Assumption Details"),
        assumptions_detail,

        _h2("3. ETL Interaction Steps"),
        _callout(
            "This section is for the ETL engineering team. "
            "Every step marked **ETL STEP** in the Python ETL version must be handled "
            "by the downstream ETL job. Steps marked **DB OPERATION (read-only)** "
            "do not require ETL involvement."
        ),
        doc.etl_interaction_steps,

        _h2("4. Python Code Architecture"),
        _h3("4.1 Database Version (python_db/)"),
        doc.python_db_architecture,
        _h3("4.2 ETL / File Version (python_etl/)"),
        doc.python_etl_architecture,

        _h3("4.3 Transformed File Index"),
        file_list,
        "",
        notes_table,

        _h2("5. Database Operations"),
        doc.database_operations,

        _h2("6. Business Logic"),
        doc.business_logic,

        _h2("7. Data Flow"),
        doc.data_flow,

        _h2("8. Decision Points"),
        doc.decision_points,

        _h2("9. Systems and Components"),
        doc.systems_and_components,

        _h2("10. Appendix"),
        doc.appendix,
    ]

    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def _h1(text: str) -> str:
    return f"# {text}"


def _h2(text: str) -> str:
    return f"## {text}"


def _h3(text: str) -> str:
    return f"### {text}"


def _callout(text: str) -> str:
    wrapped = textwrap.fill(text, width=100)
    return f"> {wrapped}"


def _toc() -> str:
    entries = [
        ("1",    "Overview"),
        ("2",    "Assumptions"),
        ("2.1",  "Per-File Assumption Details"),
        ("3",    "ETL Interaction Steps"),
        ("4",    "Python Code Architecture"),
        ("4.1",  "Database Version"),
        ("4.2",  "ETL / File Version"),
        ("4.3",  "Transformed File Index"),
        ("5",    "Database Operations"),
        ("6",    "Business Logic"),
        ("7",    "Data Flow"),
        ("8",    "Decision Points"),
        ("9",    "Systems and Components"),
        ("10",   "Appendix"),
    ]
    lines = []
    for num, title in entries:
        depth = num.count(".") + 1
        indent = "  " * (depth - 1)
        anchor = title.lower().replace(" ", "-").replace("/", "").replace("(", "").replace(")", "")
        lines.append(f"{indent}- [{num}. {title}](#{anchor})")
    return "\n".join(lines)
