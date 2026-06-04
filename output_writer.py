"""Write transformation pipeline output to disk.

Produces:
  <output_dir>/
    python_db/          ← one .py file per COBOL source (DB version)
    python_etl/         ← one .py file per COBOL source (ETL/file version)
    documentation.md    ← comprehensive technical documentation (Markdown)
    etl_operations.csv  ← flat inventory of all detected ETL operations

Usage:

    writer = OutputWriter(output_dir=Path("output"))
    report = writer.write(pipeline_output)
    print(report)
"""

import csv
import re
import textwrap
from datetime import datetime
from pathlib import Path

from transformation_pipeline import TransformationOutput, TransformationDocument
from cobol_transformer import PythonTransformationResult
from etl_detector import ETLOperation


class OutputWriter:
    """Write a TransformationOutput to structured files on disk."""

    def __init__(self, output_dir: Path | str = Path("output")):
        self.output_dir = Path(output_dir).resolve()

    def write(self, output: TransformationOutput) -> str:
        """
        Write all outputs and return a plain-text summary of what was written.
        Failures for individual files are reported in the summary rather than
        raising, so a single bad file does not prevent the rest from writing.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "python_db").mkdir(exist_ok=True)
        (self.output_dir / "python_etl").mkdir(exist_ok=True)

        written: list[str] = []
        failed: list[str] = []

        # ── Python source files ───────────────────────────────────────────────
        if not output.transformations:
            failed.append("python_db/ and python_etl/ — no transformations in pipeline output")
        else:
            for result in output.transformations:
                for subdir, code in (
                    ("python_db",  result.python_db_code),
                    ("python_etl", result.python_etl_code),
                ):
                    path, err = self._write_python_file(result, subdir, code)
                    if err:
                        failed.append(f"{path} — {err}")
                    else:
                        written.append(str(path))

        # ── ETL operations CSV ────────────────────────────────────────────────
        csv_path, err = self._write_etl_csv(output.all_etl_operations)
        if err:
            failed.append(f"{csv_path} — {err}")
        else:
            written.append(str(csv_path))

        # ── Markdown documentation ────────────────────────────────────────────
        md_path, err = self._write_markdown(output.documentation, output.transformations)
        if err:
            failed.append(f"{md_path} — {err}")
        else:
            written.append(str(md_path))

        return _format_write_report(self.output_dir, written, failed)

    # ── Writers ──────────────────────────────────────────────────────────────

    def _write_python_file(
        self,
        result: PythonTransformationResult,
        subdir: str,
        code: str,
    ) -> tuple[Path, str | None]:
        stem = _safe_stem(result.source_file)
        dest = self.output_dir / subdir / f"{stem}.py"
        try:
            content = code.strip() if code and code.strip() else (
                f"# WARNING: LLM returned empty output for {result.source_file}\n"
                f"# Re-run the pipeline or check logs/llm_calls.log for details.\n"
            )
            dest.write_text(content, encoding="utf-8", newline="\n")
            return dest, None
        except Exception as exc:
            return dest, str(exc)

    def _write_etl_csv(
        self, ops: list[ETLOperation]
    ) -> tuple[Path, str | None]:
        dest = self.output_dir / "etl_operations.csv"
        try:
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
            return dest, None
        except Exception as exc:
            return dest, str(exc)

    def _write_markdown(
        self,
        doc: TransformationDocument | None,
        transformations: list[PythonTransformationResult],
    ) -> tuple[Path, str | None]:
        dest = self.output_dir / "documentation.md"
        try:
            if doc is None:
                content = (
                    "# Documentation\n\n"
                    "> **Warning:** The documentation builder did not produce output. "
                    "Check `logs/llm_calls.log` for details.\n"
                )
            else:
                content = _build_markdown(doc, transformations)
            dest.write_text(content, encoding="utf-8", newline="\n")
            return dest, None
        except Exception as exc:
            return dest, str(exc)


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
        f"`python_db/{_safe_stem(t.source_file)}.py` / "
        f"`python_etl/{_safe_stem(t.source_file)}.py`"
        for t in transformations
    ) or "_No files transformed._"

    # Transformation notes table
    notes_rows = [
        f"| `{_safe_stem(t.source_file)}` "
        f"| {len([op for op in t.etl_operations if op.is_read])} "
        f"| {len([op for op in t.etl_operations if not op.is_read])} "
        f"| {'Yes' if 'Source truncated   : Yes' in t.transformation_notes else 'No'} |"
        for t in transformations
    ]
    notes_table = (
        "| Module | Read Ops | Write/Modify Ops | Source Truncated |\n"
        "|--------|----------|------------------|------------------|\n"
        + "\n".join(notes_rows)
    ) if notes_rows else "_No transformations performed._"

    # Per-file assumptions
    assumptions_per_file = [
        f"**{_safe_stem(t.source_file)}**\n\n" + "\n".join(f"- {a}" for a in t.assumptions)
        for t in transformations if t.assumptions
    ]
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
        doc.overview or "_Not generated._",
        _h2("2. Assumptions"),
        _callout(
            "The COBOL source may not fully reveal the beginning or end of the business "
            "process. The assumptions below represent best-guess inferences made during "
            "transformation. They should be validated with the original system owners."
        ),
        doc.assumptions or "_Not generated._",
        _h3("2.1 Per-File Assumption Details"),
        assumptions_detail,
        _h2("3. ETL Interaction Steps"),
        _callout(
            "This section is for the ETL engineering team. Every step marked "
            "ETL STEP in the Python ETL version must be handled by the downstream "
            "ETL job. Steps marked DB OPERATION (read-only) do not require ETL involvement."
        ),
        doc.etl_interaction_steps or "_Not generated._",
        _h2("4. Python Code Architecture"),
        _h3("4.1 Database Version (python_db/)"),
        doc.python_db_architecture or "_Not generated._",
        _h3("4.2 ETL / File Version (python_etl/)"),
        doc.python_etl_architecture or "_Not generated._",
        _h3("4.3 Transformed File Index"),
        file_list,
        "",
        notes_table,
        _h2("5. Database Operations"),
        doc.database_operations or "_Not generated._",
        _h2("6. Business Logic"),
        doc.business_logic or "_Not generated._",
        _h2("7. Data Flow"),
        doc.data_flow or "_Not generated._",
        _h2("8. Decision Points"),
        doc.decision_points or "_Not generated._",
        _h2("9. Systems and Components"),
        doc.systems_and_components or "_Not generated._",
        _h2("10. Appendix"),
        doc.appendix or "_Not generated._",
    ]

    return "\n\n".join(s for s in sections if s is not None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_stem(source_file: str) -> str:
    """
    Extract a safe lowercase filename stem from a source file path string.
    Works correctly regardless of whether the path uses forward or back slashes
    (i.e. safe when paths were generated on a different OS).
    """
    # Split on both separators to handle Windows paths on Linux and vice-versa
    basename = re.split(r"[\\/]", source_file.strip())[-1]
    # Strip the extension
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return stem.lower()


def _h1(text: str) -> str:
    return f"# {text}"


def _h2(text: str) -> str:
    return f"## {text}"


def _h3(text: str) -> str:
    return f"### {text}"


def _callout(text: str) -> str:
    """Render a Markdown blockquote, correctly prefixing every wrapped line."""
    wrapped = textwrap.fill(text, width=98)
    return "\n".join(f"> {line}" for line in wrapped.splitlines())


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
        anchor = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-").replace("/", "-"))
        lines.append(f"{indent}- [{num}. {title}](#{anchor})")
    return "\n".join(lines)


def _format_write_report(
    output_dir: Path, written: list[str], failed: list[str]
) -> str:
    lines = [f"Output directory: {output_dir}"]
    if written:
        lines.append(f"\nWritten ({len(written)} files):")
        for p in written:
            lines.append(f"  ✓  {p}")
    if failed:
        lines.append(f"\nFailed ({len(failed)}):")
        for msg in failed:
            lines.append(f"  ✗  {msg}")
    if not written and not failed:
        lines.append("\nNothing was written — pipeline output was empty.")
    return "\n".join(lines)
