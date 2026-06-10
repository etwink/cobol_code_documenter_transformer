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
from etl_detector import ETLDetector, ETLOperation, OperationType


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

    # gpt-5-mini limits: 272k input tokens, 128k output tokens (shared with reasoning).
    # At ~4 chars/token, 272k tokens ≈ 1.09M chars of input capacity.
    # We reserve ~50k chars for the prompt template, context blocks, and system map,
    # leaving ~150k chars of safe COBOL source per single call.
    _MAX_SOURCE_CHARS = 150_000   # hard cap for a single LLM call

    # Files larger than this are split into overlapping chunks rather than truncated.
    # Each chunk re-includes the full DATA DIVISION so data definitions are always
    # in scope, and the PROCEDURE DIVISION is divided at natural boundaries.
    _CHUNK_THRESHOLD = 150_000    # chars — above this, use chunked path
    _CHUNK_SIZE      =  80_000    # target chars per chunk (PROCEDURE section only)
    _CHUNK_OVERLAP   =   3_000    # chars of overlap carried into the next chunk

    def __init__(self):
        self.llm = AzureLLMClient()
        self.detector = ETLDetector()

    def transform(
        self,
        cobol_path: Path,
        dependency_context: str = "",
        documentation_context: str = "",
        system_context: str = "",
        known_interfaces: str = "",
    ) -> PythonTransformationResult:
        """Transform one COBOL file into both Python variants.

        Each LLM call is attempted independently.  If one fails the others
        still run and the error is embedded as a comment in the output file
        rather than crashing the pipeline.

        system_context:    a map of all programs in the system with their Python
                           module names — used to generate correct inter-module imports.
        known_interfaces:  extracted function signatures of already-transformed
                           dependencies so the LLM uses exact call signatures.
        """
        source = _read_cobol(cobol_path)
        # ETL detection always runs on the full source (regex, no token limit)
        etl_ops = self.detector.extract(source)

        chunks = self._split_into_chunks(source)
        is_chunked = len(chunks) > 1
        # Truncation only applies to the non-chunked path (single call > limit)
        was_truncated = not is_chunked and len(source) > self._MAX_SOURCE_CHARS

        common_args = (cobol_path.name, dependency_context, documentation_context, system_context)

        if is_chunked:
            db_code  = self._transform_chunked("db",  chunks, *common_args, etl_ops=etl_ops, known_interfaces=known_interfaces)
            etl_code = self._transform_chunked("etl", chunks, *common_args, etl_ops=etl_ops, known_interfaces=known_interfaces)
        else:
            src = source[: self._MAX_SOURCE_CHARS]
            db_code = self._call_llm(
                self._generate_db_version,
                src, *common_args, known_interfaces, was_truncated,
                label="DB version",
            )
            etl_code = self._call_llm(
                self._generate_etl_version,
                src, cobol_path.name, dependency_context, documentation_context,
                etl_ops, system_context, known_interfaces, was_truncated,
                label="ETL version",
            )

        assumptions = self._call_assumptions(source[:8_000], *common_args[:3])
        notes = _build_transformation_notes(cobol_path.name, etl_ops, was_truncated, is_chunked, len(chunks))

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
    # Chunking — for files that exceed _CHUNK_THRESHOLD
    # ------------------------------------------------------------------

    def _split_into_chunks(self, source: str) -> list[str]:
        """
        Split large COBOL source into overlapping chunks at natural boundaries.

        Strategy:
          1. Find the PROCEDURE DIVISION marker.  Everything before it (IDENTIFICATION,
             ENVIRONMENT, DATA divisions) is the "header block" and is prepended to
             EVERY chunk so data definitions are always in scope.
          2. Split the PROCEDURE DIVISION at section or paragraph boundaries.
          3. Consecutive chunks share _CHUNK_OVERLAP characters so the LLM has
             context about the code that immediately preceded each chunk.

        Falls back to plain character-based splitting if no PROCEDURE DIVISION
        or no paragraph markers are found.
        """
        if len(source) <= self._CHUNK_THRESHOLD:
            return [source]

        # Locate the PROCEDURE DIVISION
        proc_match = re.search(
            r"^\s{0,6}PROCEDURE\s+DIVISION\b",
            source,
            re.IGNORECASE | re.MULTILINE,
        )
        if not proc_match:
            return self._character_chunks(source)

        header    = source[: proc_match.start()]   # DATA DIVISION and above
        procedure = source[proc_match.start():]    # PROCEDURE DIVISION onwards

        # If the header alone exceeds the chunk size, just do character chunks
        if len(header) >= self._CHUNK_SIZE:
            return self._character_chunks(source)

        available = self._CHUNK_SIZE - len(header)

        # Find paragraph / section boundaries within the PROCEDURE DIVISION
        # Matches lines like "100-PROCESS.", "200-VALIDATE SECTION.", "EXIT."
        boundary_re = re.compile(
            r"^[ \t]{0,6}[A-Z0-9][A-Z0-9\-]{1,}(?:\s+SECTION)?\.",
            re.MULTILINE | re.IGNORECASE,
        )
        boundaries = [m.start() for m in boundary_re.finditer(procedure)]

        if not boundaries:
            return self._character_chunks(source)

        chunks: list[str] = []
        proc_start = 0

        while proc_start < len(procedure):
            target_end = proc_start + available

            if target_end >= len(procedure):
                # Last chunk — include everything remaining
                chunk_proc = procedure[max(0, proc_start - self._CHUNK_OVERLAP):]
                chunks.append(header + chunk_proc)
                break

            # Find the nearest boundary at or before target_end
            best = target_end
            for b in boundaries:
                if proc_start < b <= target_end:
                    best = b   # keep taking; last one wins = closest to target

            chunk_proc = procedure[max(0, proc_start - self._CHUNK_OVERLAP): best]
            chunks.append(header + chunk_proc)
            proc_start = best

        return chunks if chunks else [source]

    def _character_chunks(self, source: str) -> list[str]:
        """Plain character-based fallback chunker with overlap."""
        chunks: list[str] = []
        start = 0
        while start < len(source):
            end = min(start + self._CHUNK_SIZE, len(source))
            chunks.append(source[start: end])
            if end == len(source):
                break
            start = end - self._CHUNK_OVERLAP
        return chunks

    def _transform_chunked(
        self,
        version: str,               # "db" or "etl"
        chunks: list[str],
        filename: str,
        dep_ctx: str,
        doc_ctx: str,
        sys_ctx: str,
        etl_ops: list[ETLOperation],
        *,
        known_interfaces: str = "",
    ) -> str:
        """Transform each chunk independently then synthesize into one module."""
        chunk_results: list[str] = []
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            chunk_label = f"chunk {i+1}/{total}"
            if version == "db":
                code = self._call_llm(
                    self._generate_db_version,
                    chunk, filename, dep_ctx, doc_ctx, sys_ctx, known_interfaces, False,
                    label=f"DB version {chunk_label}",
                )
            else:
                code = self._call_llm(
                    self._generate_etl_version,
                    chunk, filename, dep_ctx, doc_ctx, etl_ops, sys_ctx, known_interfaces, False,
                    label=f"ETL version {chunk_label}",
                )
            chunk_results.append(code)

        if len(chunk_results) == 1:
            return chunk_results[0]

        return self._call_llm(
            self._synthesize_chunks,
            chunk_results, filename, version,
            label=f"{version.upper()} synthesis",
        )

    def _synthesize_chunks(
        self,
        chunk_results: list[str],
        filename: str,
        version: str,
    ) -> str:
        """Merge chunk-by-chunk generated Python into one coherent module."""
        marker = "# DB_OPERATION:" if version == "db" else "# ETL_INPUT:\n# ETL_OUTPUT:"
        blocks = "\n\n".join(
            f"# ── CHUNK {i+1} OF {len(chunk_results)} ──\n{code}"
            for i, code in enumerate(chunk_results)
        )
        prompt = (
            f"You are merging {len(chunk_results)} Python code chunks generated from "
            f"different sections of one large COBOL file ({filename}) into a single "
            f"complete, runnable Python module.\n\n"
            f"Each chunk was generated from a different portion of the PROCEDURE DIVISION. "
            f"They share the same DATA DIVISION, so data structure definitions and imports "
            f"may be duplicated across chunks.\n\n"
            f"Chunks:\n{blocks}\n\n"
            f"Merge rules:\n"
            f"1. One set of imports at the top — deduplicate, keep all unique ones.\n"
            f"2. One module-level docstring summarising the full program.\n"
            f"3. Keep ALL functions and ALL business logic from every chunk. "
            f"   Do not omit anything.\n"
            f"4. If the same function appears in multiple chunks, keep the most complete "
            f"   version (typically the one from the later chunk which has more context).\n"
            f"5. Ensure the entry point (main() or equivalent) calls all processing "
            f"   steps in the correct sequence.\n"
            f"6. Preserve every {marker} comment exactly as written.\n"
            f"7. If this is the ETL version, keep the full # ── ETL CONTRACT ── block "
            f"   from whichever chunk has the most complete version.\n\n"
            f"Output ONLY valid Python code. No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=10_000)

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
        known_interfaces: str,
        was_truncated: bool,
    ) -> str:
        dep_block   = f"\n\nDependency context (which programs this one CALLs or COPYs):\n{dep_ctx}" if dep_ctx else ""
        doc_block   = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        sys_block   = f"\n\nSYSTEM MAP — all programs in this system and their Python module names:\n{sys_ctx}" if sys_ctx else ""
        iface_block = f"\n\n{known_interfaces}" if known_interfaces else ""
        trunc_note  = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call. "
            "Remaining sections are represented by the stub below."
            if was_truncated else ""
        )

        prompt = f"""You are a senior developer converting a legacy COBOL program to Python 3.11.

COBOL file: {filename}{dep_block}{doc_block}{sys_block}{iface_block}

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

INTER-MODULE CALLS
- When calling a function in another module, use EXACTLY the signature shown in the
  KNOWN DEPENDENCY INTERFACES block above. Match parameter names, types, and order precisely.
- If no interface is shown for a dependency (it is marked "not yet transformed"), add
  # TODO: verify signature on the call line and use your best inference from the CALL USING clause.

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

        return self.llm.query(prompt, max_tokens=64_000)

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
        known_interfaces: str,
        was_truncated: bool,
    ) -> str:
        dep_block   = f"\n\nDependency context:\n{dep_ctx}" if dep_ctx else ""
        doc_block   = f"\n\nBusiness documentation context:\n{doc_ctx[:3000]}" if doc_ctx else ""
        sys_block   = f"\n\nSYSTEM MAP:\n{sys_ctx}" if sys_ctx else ""
        iface_block = f"\n\n{known_interfaces}" if known_interfaces else ""
        trunc_note  = (
            "\n\n# NOTE: The COBOL source was truncated for the LLM call."
            if was_truncated else ""
        )

        # SQL_CURSOR (OPEN/FETCH/CLOSE on a cursor name) is excluded — the actual table
        # is already listed as a SQL_SELECT from the DECLARE CURSOR FOR SELECT statement.
        # Including cursor names would tell the LLM to read from etl_in_<cursor_name>.txt
        # instead of the real table's input file.
        read_ops  = [op for op in etl_ops if op.is_read and op.operation_type != OperationType.SQL_CURSOR]
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

COBOL file: {filename}{dep_block}{doc_block}{sys_block}{iface_block}

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

INTER-MODULE CALLS
- When calling a function in another module, use EXACTLY the signature shown in the
  KNOWN DEPENDENCY INTERFACES block above. Match parameter names, types, and order precisely.
- If no interface is shown for a dependency (it is marked "not yet transformed"), add
  # TODO: verify signature on the call line and use your best inference from the CALL USING clause.

STRUCTURE AND BUSINESS LOGIC
- Identical structure to the DB version (docstring, snake_case, type hints, functions).
- ALL business logic (calculations, conditions, loops, validations) must be identical.
  Only the I/O layer changes — a database call becomes a file read or write.

OUTPUT
Output ONLY valid Python code. No markdown fences. No explanatory prose outside
docstrings and inline comments."""

        return self.llm.query(prompt, max_tokens=64_000)

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
    was_chunked: bool = False,
    chunk_count: int = 1,
) -> str:
    reads  = [op for op in etl_ops if op.is_read]
    writes = [op for op in etl_ops if not op.is_read]

    if was_chunked:
        size_note = f"Chunked — split into {chunk_count} chunks (DATA DIVISION shared across all)"
    elif was_truncated:
        size_note = "Truncated — source exceeded single-call limit; output may be incomplete"
    else:
        size_note = "No"

    lines = [
        f"Source file        : {filename}",
        f"Source truncated   : {size_note}",
        f"ETL ops detected   : {len(etl_ops)} total ({len(reads)} read, {len(writes)} write/modify)",
    ]
    if writes:
        lines.append("")
        lines.append("Write/modify operations (become ETL staging steps in the file version):")
        for op in writes:
            lines.append(
                f"  Line {op.line_number:>5}: [{op.operation_type.value:16s}] "
                f"{op.description}  →  etl_out_{op.table_or_file.lower()}.txt"
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
