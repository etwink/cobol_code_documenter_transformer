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

        # ── Per-version package files ─────────────────────────────────────────
        # Derive the authoritative entry point from cluster summaries — the
        # program that is never called by any other program in the dependency
        # graph.  This is more reliable than searching generated code for
        # "if __name__ == '__main__':" which the LLM may or may not produce.
        entry_point = next(
            (cs.entry_point.lower() for cs in output.cluster_summaries if cs.entry_point),
            None,
        )
        for path, err in self._write_db_package_files(output.transformations, entry_point):
            (failed if err else written).append(str(path) + (f" — {err}" if err else ""))
        for path, err in self._write_etl_package_files(output.transformations, output.all_etl_operations, entry_point):
            (failed if err else written).append(str(path) + (f" — {err}" if err else ""))

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

    # ── Per-version package files ────────────────────────────────────────────

    def _write_db_package_files(
        self,
        transformations: list[PythonTransformationResult],
        entry_point: str | None,
    ) -> list[tuple[Path, str | None]]:
        db_dir = self.output_dir / "python_db"
        return [
            _safe_write(db_dir / "__init__.py",      _build_db_init(transformations)),
            _safe_write(db_dir / "requirements.txt",  _DB_REQUIREMENTS),
            _safe_write(db_dir / "quickstart.md",    _build_db_quickstart(transformations, entry_point)),
        ]

    def _write_etl_package_files(
        self,
        transformations: list[PythonTransformationResult],
        all_etl_ops: list[ETLOperation],
        entry_point: str | None,
    ) -> list[tuple[Path, str | None]]:
        etl_dir = self.output_dir / "python_etl"
        return [
            _safe_write(etl_dir / "__init__.py",      _build_etl_init(transformations)),
            _safe_write(etl_dir / "requirements.txt",  _ETL_REQUIREMENTS),
            _safe_write(etl_dir / "quickstart.md",    _build_etl_quickstart(transformations, all_etl_ops, entry_point)),
        ]

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


# ---------------------------------------------------------------------------
# Per-version package file builders
# ---------------------------------------------------------------------------

_DB_REQUIREMENTS = """\
# Dependencies for the Database Version (python_db/)
# Install with: pip install -r requirements.txt

sqlalchemy>=2.0.0
python-dotenv>=1.0.0

# Optional: raw ODBC connectivity as an alternative to SQLAlchemy
# pyodbc>=5.0.0
"""

_ETL_REQUIREMENTS = """\
# Dependencies for the ETL Version (python_etl/)
# Install with: pip install -r requirements.txt
#
# This version has NO database dependencies.
# All database interaction is handled by the external ETL environment.

python-dotenv>=1.0.0
"""


def _safe_write(dest: Path, content: str) -> tuple[Path, str | None]:
    try:
        dest.write_text(content, encoding="utf-8", newline="\n")
        return dest, None
    except Exception as exc:
        return dest, str(exc)


def _build_db_init(transformations: list[PythonTransformationResult]) -> str:
    stems = [_safe_stem(t.source_file) for t in transformations]
    imports = "\n".join(f"from . import {s}" for s in stems)
    names   = ", ".join(f'"{s}"' for s in stems)
    return (
        '"""Database version package — all modules are part of one connected system."""\n\n'
        f"{imports}\n\n"
        f"__all__ = [{names}]\n"
    )


def _build_etl_init(transformations: list[PythonTransformationResult]) -> str:
    stems = [_safe_stem(t.source_file) for t in transformations]
    imports = "\n".join(f"from . import {s}" for s in stems)
    names   = ", ".join(f'"{s}"' for s in stems)
    return (
        '"""ETL version package — all modules are part of one connected system.\n\n'
        "No database connections exist in this version.  All DB interaction is\n"
        'handled by the external ETL environment via pipe-delimited .txt files."""\n\n'
        f"{imports}\n\n"
        f"__all__ = [{names}]\n"
    )


def _build_db_quickstart(
    transformations: list[PythonTransformationResult],
    entry_point: str | None,
) -> str:
    modules = [_safe_stem(t.source_file) for t in transformations]
    # Use the dependency-graph entry point (program never called by others).
    # Fall back to the first module alphabetically only when no entry point
    # could be determined from the dependency graph.
    entry = entry_point or (modules[0] if modules else "<entry_module>")
    module_list = "\n".join(f"- `{m}.py`" for m in modules)

    return f"""\
# Database Version — Quickstart

## What This Is

This is the **database version** of the converted system. It behaves identically to the
original COBOL — all reads and writes go directly to the database via SQLAlchemy.

## Prerequisites

- Python 3.11+
- A database reachable via SQLAlchemy connection string (SQL Server, DB2, PostgreSQL, etc.)
- Access credentials for the target database

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in `python_db/` (or set environment variables):

```ini
DB_CONNECTION_STRING=mssql+pyodbc://user:password@server/database?driver=ODBC+Driver+17+for+SQL+Server
```

The `get_db_session()` function in each module reads `DB_CONNECTION_STRING` at startup.

## Running

```bash
# Run as a package from the parent output/ directory:
python -m python_db.{entry}

# Or run the entry module directly:
python python_db/{entry}.py
```

## Modules in This System

{module_list}

All modules are part of one Python package. They call each other via relative imports
(`from . import <module>`). Do not run utility/subprogram modules directly — run the
entry point above, which orchestrates the full flow.

## Navigating the Code

- Search for `# DB_OPERATION:` to find every database call.
- Each module's docstring describes its purpose, inputs, outputs, and call dependencies.
"""


def _build_etl_quickstart(
    transformations: list[PythonTransformationResult],
    all_etl_ops: list[ETLOperation],
    entry_point: str | None,
) -> str:
    modules = [_safe_stem(t.source_file) for t in transformations]
    entry = entry_point or (modules[0] if modules else "<entry_module>")
    module_list = "\n".join(f"- `{m}.py`" for m in modules)

    read_ops  = [op for op in all_etl_ops if op.is_read]
    write_ops = [op for op in all_etl_ops if not op.is_read]

    input_files = "\n".join(
        f"- `etl_in_{op.table_or_file.lower()}.txt` — {op.description}"
        for op in read_ops
    ) or "_None detected._"

    output_files = "\n".join(
        f"- `etl_out_{op.table_or_file.lower()}.txt` — {op.description}"
        for op in write_ops
    ) or "_None detected._"

    etl_job_specs = []
    for op in read_ops:
        fname = f"etl_in_{op.table_or_file.lower()}.txt"
        etl_job_specs.append(
            f"**Input job for `{fname}`**\n"
            f"- Query: `SELECT <columns> FROM {op.table_or_file}`\n"
            f"- Write results to `{fname}` as pipe-delimited UTF-8 text with a header row.\n"
            f"- This file **must exist before** the Python module runs.\n"
        )
    for op in write_ops:
        fname = f"etl_out_{op.table_or_file.lower()}.txt"
        etl_job_specs.append(
            f"**Output job for `{fname}`**\n"
            f"- Read `{fname}` (pipe-delimited, header row).\n"
            f"- Perform: `{op.operation_type.value}` on `{op.table_or_file}`.\n"
            f"- This file is produced **after** the Python module runs.\n"
        )
    etl_specs_block = "\n\n".join(etl_job_specs) or "_No ETL jobs required for this system._"

    return f"""\
# ETL Version — Quickstart

## What This Is

This is the **ETL version** of the converted system. It has **zero database connections**.
All database interaction is delegated to your ETL environment. The Python code communicates
with ETL exclusively through pipe-delimited `.txt` files.

## How It Works

```
ETL environment          Python (this code)        ETL environment
─────────────────        ──────────────────        ─────────────────
Queries DB tables   →    Reads etl_in_*.txt
                         Runs business logic
                         Writes etl_out_*.txt  →   Inserts/updates DB tables
```

## Prerequisites

- Python 3.11+
- No database driver needed

## Installation

```bash
pip install -r requirements.txt
```

## File Format

All ETL files use **pipe-delimited UTF-8 text** with a header row:

```
COLUMN_ONE|COLUMN_TWO|COLUMN_THREE
value1|value2|value3
```

## Before Running — Input Files Required

The ETL environment must create these files **before** running the Python code:

{input_files}

Place them in the same directory as the Python modules (or set the path in your config).

## Running

```bash
# Run as a package from the parent output/ directory:
python -m python_etl.{entry}

# Or run the entry module directly:
python python_etl/{entry}.py
```

## After Running — Output Files Produced

The Python code writes these files for the ETL environment to process:

{output_files}

## Modules in This System

{module_list}

## ETL Job Specifications

For each file, an ETL job must be created in your ETL environment:

{etl_specs_block}

## Navigating the Code

- Search for `# ETL_INPUT:` to find every place an input file is read.
- Search for `# ETL_OUTPUT:` to find every place an output file is written.
- The `# ── ETL CONTRACT ──` block at the top of each module lists all files for that module.
"""


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
