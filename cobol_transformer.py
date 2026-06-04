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
        system_context: str = "",
    ) -> PythonTransformationResult:
        """Transform one COBOL file into both Python variants.

        Each LLM call is attempted independently.  If one fails the others
        still run and the error is embedded as a comment in the output file
        rather than crashing the pipeline.

        system_context: a map of all programs in the system with their Python
        module names — used to generate correct inter-module imports.
        """
        source = _read_cobol(cobol_path)
        etl_ops = self.detector.extract(source)

        truncated = source[: self._MAX_SOURCE_CHARS]
        was_truncated = len(source) > self._MAX_SOURCE_CHARS

        db_code = self._call_llm(
            self._generate_db_version,
            truncated, cobol_path.name, dependency_context, documentation_context,
            system_context, was_truncated,
            label="DB version",
        )
        etl_code = self._call_llm(
            self._generate_etl_version,
            truncated, cobol_path.name, dependency_context, documentation_context,
            etl_ops, system_context, was_truncated,
            label="ETL version",
        )
        assumptions = self._call_assumptions(
            truncated, cobol_path.name, dependency_context, documentation_context
        )
        notes = _build_transformation_notes(cobol_path.name, etl_ops, was_truncated)

        return PythonTransformationResult(
            source_file=str(cobol_path),
            python_db_code=db_code,
            python_etl_code=etl_code,
            etl_operations=etl_ops,
            assumptions=assumptions,
            transformation_notes=notes,
        )

    def _call_llm(self, fn, *args, label: str = "") -> str:
        """Call an LLM generation method, returning an error comment on failure."""
        try:
            return fn(*args)
        except Exception as exc:
            return (
                f"# ERROR generating {label} for this file\n"
                f"# {type(exc).__name__}: {exc}\n"
                f"# Check logs/llm_calls.log for the full request/response.\n"
            )

    def _call_assumptions(self, *args) -> list[str]:
        """Call the assumptions extractor, returning an empty list on failure."""
        try:
            return self._extract_assumptions(*args)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # DB version
    # ------------------------------------------------------------------

    def _generate_db_version(
        self,
        source: str,
        filename: str,
        dep_ctx: str,
        doc_ctx: str,
        sys_ctx: str,
        was_truncated: bool,
    ) -> str:
        dep_block = f"\n\nDependency context (which programs this one CALLs or COPYs):\n{dep_ctx}" if dep_ctx else ""
        doc_block = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        sys_block = f"\n\nSYSTEM MAP — all programs in this system and their Python module names:\n{sys_ctx}" if sys_ctx else ""
        trunc_note = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call. "
            "Remaining sections are represented by the stub below."
            if was_truncated else ""
        )

        prompt = f"""You are a senior developer converting a legacy COBOL program to Python 3.11.

COBOL file: {filename}{dep_block}{doc_block}{sys_block}

COBOL SOURCE:{trunc_note}
{source}

=== TASK: DATABASE VERSION ===

Convert this COBOL program to idiomatic Python. This version retains all database
and file operations as real database calls. Follow these rules exactly:

CONNECTED SYSTEM — PACKAGE STRUCTURE
- All COBOL programs in this scan are part of ONE system. The generated Python files
  are modules in the same package. Treat them as such.
- For every COBOL CALL statement, use a relative import: from . import <module_name>
  and call the appropriate function in that module. Do NOT duplicate logic.
- For COBOL COPY statements (copybooks), import shared data structures or constants
  from the corresponding Python module: from . import <copybook_module>
- If this program is the system entry point, add an if __name__ == "__main__": block
  that calls main().
- If this is a called subprogram or utility, expose its primary logic as a
  clearly named public function (not wrapped in __main__).

STRUCTURE
- Produce a single Python module (.py file).
- Add a module-level docstring covering: program purpose, key inputs, key outputs,
  entry point function name, and which other modules it calls or is called by.
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
- Group the SQLAlchemy engine/session setup in a shared helper; if another module in
  the system already defines get_db_session(), import it instead of redefining it.

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
        sys_ctx: str,
        was_truncated: bool,
    ) -> str:
        dep_block = f"\n\nDependency context:\n{dep_ctx}" if dep_ctx else ""
        doc_block = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        sys_block = f"\n\nSYSTEM MAP:\n{sys_ctx}" if sys_ctx else ""
        trunc_note = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call."
            if was_truncated else ""
        )

        read_ops  = [op for op in etl_ops if op.is_read]
        write_ops = [op for op in etl_ops if not op.is_read]

        read_block = (
            "\n".join(
                f"  Line {op.line_number}: [{op.operation_type.value}] {op.description}"
                f"  → input file: etl_in_{op.table_or_file.lower()}.txt"
                for op in read_ops
            ) or "  (none detected by static analysis)"
        )
        write_block = (
            "\n".join(
                f"  Line {op.line_number}: [{op.operation_type.value}] {op.description}"
                f"  → output file: etl_out_{op.table_or_file.lower()}.txt"
                for op in write_ops
            ) or "  (none detected by static analysis)"
        )

        prompt = f"""You are a senior developer converting a legacy COBOL program to Python 3.11.

COBOL file: {filename}{dep_block}{doc_block}{sys_block}

Database READ operations detected (each becomes a file read from ETL-provided input):
{read_block}

Database WRITE operations detected (each becomes a file write for ETL to process):
{write_block}

COBOL SOURCE:{trunc_note}
{source}

=== TASK: ETL VERSION — FULLY FILE-BASED, NO DATABASE CONNECTIONS ===

This Python version has ZERO database connections. The ETL environment is responsible
for all database interaction. The Python code communicates with the ETL environment
exclusively through pipe-delimited text files.

FILE FORMAT — ALL FILES MUST USE THIS FORMAT:
- Delimiter  : pipe character  |
- File extension: .txt
- Encoding   : UTF-8
- First row  : column headers (pipe-delimited)
- Subsequent rows: data rows
- Example row: POLICY_ID|CUSTOMER_ID|EFFECTIVE_DATE|AMOUNT
               10042|9981|2026-01-15|1250.00

ETL INPUT FILES (data provided BY the ETL environment BEFORE this module runs):
- For every database READ in the original COBOL, read from a pipe-delimited input file
  named etl_in_<TABLE_NAME>.txt instead of querying the database.
- At module start, raise FileNotFoundError with a clear message if a required input
  file is missing.
- Mark each file read with:
    # ETL_INPUT: <filename> — provided by ETL job "<job description>"

ETL OUTPUT FILES (data produced BY this module FOR the ETL environment AFTER it runs):
- For every database INSERT / UPDATE / DELETE, write a pipe-delimited output file
  named etl_out_<TABLE_NAME>.txt.
- Collect all rows in a list of dicts during processing, then write the file in one
  pass at the end using the csv module with delimiter="|".
- Mark each output file write with:
    # ETL_OUTPUT: <filename> — consumed by ETL job "<job description>"

MODULE-LEVEL ETL CONTRACT
Immediately below the module docstring, add this comment block — fill in every file:
    # ── ETL CONTRACT ──────────────────────────────────────────────────────────
    # INPUT FILES  (must exist before this module runs — provided by ETL):
    #   etl_in_<TABLE>.txt   | source: <TABLE> | schema: COL1|COL2|...
    #   (one line per input file)
    #
    # OUTPUT FILES (produced by this module — consumed by ETL after it runs):
    #   etl_out_<TABLE>.txt  | target: <TABLE> | operation: INSERT|UPDATE|DELETE
    #                        | schema: COL1|COL2|...
    #   (one line per output file)
    #
    # ETL JOB SPECIFICATIONS:
    #   Input  job: "<job name>" must SELECT <columns> FROM <table> WHERE <condition>
    #               and write etl_in_<TABLE>.txt before this module runs.
    #   Output job: "<job name>" must read etl_out_<TABLE>.txt and
    #               INSERT/UPDATE/DELETE <table> using the following column mapping:
    #               file_col → table_col, ...
    # ─────────────────────────────────────────────────────────────────────────

CONNECTED SYSTEM — PACKAGE STRUCTURE
- Same relative import rules as the DB version. Use from . import <module_name>
  for any COBOL CALL or COPY dependency.

STRUCTURE AND BUSINESS LOGIC
- Identical structure to the DB version (docstring, snake_case, type hints, functions).
- ALL business logic (calculations, conditions, loops, validations) must be identical.
  Only the I/O layer changes — a database call becomes a file read or write.

OUTPUT
Output ONLY valid Python code. No markdown fences. No explanatory prose outside
docstrings and inline comments."""

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
