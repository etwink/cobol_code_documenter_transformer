"""Transform COBOL source code into two Python variants via LLM.

DB version  — all database operations kept as real SQL/CICS calls.
ETL version — all write/modify operations replaced with staging file writes;
              a separate ETL job is expected to pick up and process those files.

Each variant is annotated with inline comment markers:
  # DB_OPERATION:  <description>   (in both versions for reads; DB version for writes)
  # ETL_STEP:      <description>   (ETL version only, for every staged write)
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from llm_integration import AzureLLMClient
from etl_detector import ETLDetector, ETLOperation, etl_operations_summary


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PythonTransformationResult:
    source_file: str
    python_db_code: str             # Python with real database operations
    python_etl_code: str            # Python with file-based ETL staging
    etl_operations: list[ETLOperation] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    transformation_notes: str = ""


# ---------------------------------------------------------------------------
# Transformer
# ---------------------------------------------------------------------------

class CobolToPythonTransformer:
    """Convert a COBOL source file into two Python variants using an LLM."""

    # Maximum characters of COBOL source passed to each LLM call.
    # Very large programs are summarised after this point; the full source is
    # still used for ETL detection (which is regex-based, not LLM-based).
    _MAX_SOURCE_CHARS = 14_000

    def __init__(self):
        self.llm = AzureLLMClient()
        self.detector = ETLDetector()

    def transform(
        self,
        cobol_path: Path,
        dependency_context: str = "",
        documentation_context: str = "",
    ) -> PythonTransformationResult:
        """Transform one COBOL file into both Python variants."""
        source = _read_cobol(cobol_path)
        etl_ops = self.detector.extract(source)

        truncated = source[: self._MAX_SOURCE_CHARS]
        was_truncated = len(source) > self._MAX_SOURCE_CHARS

        db_code   = self._generate_db_version(truncated, cobol_path.name, dependency_context, documentation_context, was_truncated)
        etl_code  = self._generate_etl_version(truncated, cobol_path.name, dependency_context, documentation_context, etl_ops, was_truncated)
        assumptions = self._extract_assumptions(truncated, cobol_path.name, dependency_context, documentation_context)
        notes     = _build_transformation_notes(cobol_path.name, etl_ops, was_truncated)

        return PythonTransformationResult(
            source_file=str(cobol_path),
            python_db_code=db_code,
            python_etl_code=etl_code,
            etl_operations=etl_ops,
            assumptions=assumptions,
            transformation_notes=notes,
        )

    # ------------------------------------------------------------------
    # DB version
    # ------------------------------------------------------------------

    def _generate_db_version(
        self,
        source: str,
        filename: str,
        dep_ctx: str,
        doc_ctx: str,
        was_truncated: bool,
    ) -> str:
        dep_block = f"\n\nDependency context (which programs this one CALLs or COPYs):\n{dep_ctx}" if dep_ctx else ""
        doc_block = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        trunc_note = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call. "
            "Remaining sections are represented by the stub below."
            if was_truncated else ""
        )

        prompt = f"""You are a senior developer converting a legacy COBOL program to Python 3.11.

COBOL file: {filename}{dep_block}{doc_block}

COBOL SOURCE:{trunc_note}
{source}

=== TASK: DATABASE VERSION ===

Convert this COBOL program to idiomatic Python. This version retains all database
and file operations as real database calls. Follow these rules exactly:

STRUCTURE
- Produce a single Python module (.py file).
- Add a module-level docstring covering: program purpose, key inputs, key outputs,
  entry point function name.
- Mirror the COBOL division structure through classes or clearly named functions:
  identify/initialise → validate_inputs → process → finalise.
- Use snake_case for all identifiers. Map COBOL data-names to readable equivalents
  (e.g. WS-ACCT-NUM → account_number).
- Add type hints wherever the type is unambiguous from the COBOL context.

DATABASE OPERATIONS
- For EXEC SQL: use sqlalchemy (preferred) or a raw cursor with parameterized queries.
  Never construct SQL by string concatenation.
- For EXEC CICS READ/WRITE/REWRITE/DELETE: convert to equivalent SQLAlchemy table operations.
- Mark every database call with an inline comment on its own line ABOVE the call:
    # DB_OPERATION: <one-sentence description of what this query does>
- Group the SQLAlchemy engine/session setup in a helper function get_db_session() at the
  top of the module.

BUSINESS LOGIC
- Preserve all conditional logic, calculations, loops, and validations exactly as coded.
  Do not simplify or omit any logic.
- Translate COBOL EVALUATE to Python match/case or if/elif chains.
- Translate PERFORM … UNTIL / PERFORM … VARYING to while/for loops.

ERROR HANDLING
- Translate COBOL status codes (FILE STATUS, SQLCODE) to Python exceptions or return codes.
- Wrap database calls in try/except blocks that re-raise as a descriptive RuntimeError.

OUTPUT
Output ONLY valid Python code. No markdown fences. No explanatory prose outside docstrings
and inline comments. If the source was truncated, add a stub function with a # TODO comment
for the missing portion."""

        return self.llm.query(prompt, max_tokens=10_000)

    # ------------------------------------------------------------------
    # ETL file version
    # ------------------------------------------------------------------

    def _generate_etl_version(
        self,
        source: str,
        filename: str,
        dep_ctx: str,
        doc_ctx: str,
        etl_ops: list[ETLOperation],
        was_truncated: bool,
    ) -> str:
        dep_block = f"\n\nDependency context:\n{dep_ctx}" if dep_ctx else ""
        doc_block = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        trunc_note = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call."
            if was_truncated else ""
        )

        write_ops = [op for op in etl_ops if not op.is_read]
        ops_block = (
            "\n".join(
                f"  Line {op.line_number}: [{op.operation_type.value}] {op.description}"
                f"  → staging file: etl_stage_{op.table_or_file.lower()}.csv"
                for op in write_ops
            )
            if write_ops
            else "  (none detected by static analysis — use judgment from the code)"
        )

        prompt = f"""You are a senior developer converting a legacy COBOL program to Python 3.11.

COBOL file: {filename}{dep_block}{doc_block}

Write/modify operations detected by static analysis (these MUST become ETL staging steps):
{ops_block}

COBOL SOURCE:{trunc_note}
{source}

=== TASK: ETL / FILE-STAGING VERSION ===

Convert this COBOL program to idiomatic Python. In this version all database WRITE,
INSERT, UPDATE, and DELETE operations are replaced by writes to CSV staging files so
that a downstream ETL job can process them. Read-only queries (SELECT, CICS READ) may
still access the database directly. Follow these rules exactly:

MODULE-LEVEL ETL CONTRACT
At the very top of the file (below the module docstring), add a comment block:
    # ── ETL CONTRACT ─────────────────────────────────────────────────────────
    # This module produces the following staging files for downstream ETL processing:
    #
    #   etl_stage_<TABLE>.csv  ← <operation type>  → <target table/dataset>
    #   (one line per staging file)
    #
    # Each staging file uses UTF-8 CSV with a header row.
    # The downstream ETL job must apply these files in the order listed.
    # ─────────────────────────────────────────────────────────────────────────

STRUCTURE
- Same structural rules as the DB version (docstring, functions, snake_case, type hints).
- The business logic must be IDENTICAL to the DB version — only the I/O layer changes.

WRITE OPERATIONS → STAGING FILES
- Replace every INSERT / UPDATE / DELETE / CICS WRITE / CICS REWRITE / CICS DELETE with:
    1. Build a Python dict representing the row.
    2. Append to a pandas DataFrame (or write with csv.DictWriter if pandas not available).
    3. At the end of processing, flush all DataFrames to CSV files named
       etl_stage_<TABLE_OR_FILE_NAME>.csv (lower-case, underscores for spaces).
- Mark each staging write with a comment ABOVE the operation:
    # ETL_STEP: <what data is being staged, which table it maps to, insert/update/delete>

READ OPERATIONS → KEEP AS DATABASE CALLS
- SELECT queries and CICS READ operations remain as real database lookups.
- Mark them with:
    # DB_OPERATION: <description> — read-only, no ETL staging needed

ERROR HANDLING
- Same as the DB version for read operations.
- For staging file writes: wrap in try/except and log errors; do not suppress them silently.

OUTPUT
Output ONLY valid Python code. No markdown fences. No explanatory prose outside docstrings
and inline comments."""

        return self.llm.query(prompt, max_tokens=10_000)

    # ------------------------------------------------------------------
    # Assumptions extraction
    # ------------------------------------------------------------------

    def _extract_assumptions(
        self,
        source: str,
        filename: str,
        dep_ctx: str,
        doc_ctx: str,
    ) -> list[str]:
        dep_block = f"\n\nDependency context:\n{dep_ctx}" if dep_ctx else ""
        doc_block = f"\n\nBusiness documentation:\n{doc_ctx[:2000]}" if doc_ctx else ""

        prompt = f"""You are a senior COBOL analyst preparing a technical migration document.

COBOL file: {filename}{dep_block}{doc_block}

COBOL SOURCE (excerpt):
{source[:6000]}

=== TASK: IDENTIFY ASSUMPTIONS ===

This COBOL source may be partial — it may not show what triggers this program, what
calls it, or what downstream systems consume its output. Make your best professional
judgements about the following. For each assumption state:
  - The assumption itself (one sentence)
  - The evidence in the code that supports it
  - Confidence level: HIGH | MEDIUM | LOW

Provide exactly these six assumptions (number them 1–6):

1. TRIGGER: What event or system starts this program
   (scheduler, online transaction, JCL batch step, user action, CICS terminal, etc.)

2. PRE-CONDITIONS: What must exist before this program can run successfully
   (required files open, database records present, prior jobs completed, etc.)

3. OUTPUTS: What this program produces or delivers to downstream consumers
   (files written, database rows inserted/updated, reports, messages, return codes)

4. BUSINESS PROCESS: Which business process this program belongs to
   (payroll, claims processing, account management, reporting cycle, etc.)

5. PROCESS BOUNDARIES: Where this program sits in the larger flow
   (is it an entry point, a called subroutine, a report generator, a cleanup step?)

6. MISSING CONTEXT: Any other significant information that is absent from the source
   and that a maintainer would need to fully understand this program

Format: number, label in CAPS, then the assumption text, then "Evidence: ..." and "[CONFIDENCE]".
Example:
1. TRIGGER: This program is likely scheduled as a nightly batch job. Evidence: The program opens
   a sequential input file and does not accept any CICS COMMAREA, suggesting it runs unattended.
   [HIGH]"""

        response = self.llm.query(prompt, max_tokens=3_000)
        return _parse_numbered_list(response)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_cobol(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _build_transformation_notes(
    filename: str,
    etl_ops: list[ETLOperation],
    was_truncated: bool,
) -> str:
    reads  = [op for op in etl_ops if op.is_read]
    writes = [op for op in etl_ops if not op.is_read]
    lines = [
        f"Source file        : {filename}",
        f"Source truncated   : {'Yes — output may be incomplete' if was_truncated else 'No'}",
        f"ETL ops detected   : {len(etl_ops)} total ({len(reads)} read, {len(writes)} write/modify)",
    ]
    if writes:
        lines.append("")
        lines.append("Write/modify operations (become ETL staging steps in the file version):")
        for op in writes:
            lines.append(
                f"  Line {op.line_number:>5}: [{op.operation_type.value:16s}] "
                f"{op.description}  →  etl_stage_{op.table_or_file.lower()}.csv"
            )
    return "\n".join(lines)


def _parse_numbered_list(text: str) -> list[str]:
    """Extract numbered items from an LLM response into a flat list."""
    items: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+[.)]\s+", stripped):
            if current:
                items.append(" ".join(current))
            current = [re.sub(r"^\d+[.)]\s+", "", stripped)]
        elif stripped and current:
            current.append(stripped)

    if current:
        items.append(" ".join(current))

    return items
