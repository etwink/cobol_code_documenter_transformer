"""Detect and classify database/ETL operations in COBOL source code.

Scans for EXEC SQL, EXEC CICS file operations, and sequential file I/O.
Returns a list of ETLOperation objects that drive both the code transformer
and the documentation generator.
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class OperationType(str, Enum):
    SQL_SELECT   = "SQL_SELECT"
    SQL_INSERT   = "SQL_INSERT"
    SQL_UPDATE   = "SQL_UPDATE"
    SQL_DELETE   = "SQL_DELETE"
    SQL_CURSOR   = "SQL_CURSOR"     # OPEN / FETCH / CLOSE cursor
    CICS_READ    = "CICS_READ"
    CICS_WRITE   = "CICS_WRITE"
    CICS_REWRITE = "CICS_REWRITE"
    CICS_DELETE  = "CICS_DELETE"
    FILE_OPEN    = "FILE_OPEN"
    FILE_READ    = "FILE_READ"
    FILE_WRITE   = "FILE_WRITE"
    FILE_CLOSE   = "FILE_CLOSE"


# Operations that read data; everything else is a write/modify operation
_READ_TYPES: frozenset[OperationType] = frozenset({
    OperationType.SQL_SELECT,
    OperationType.SQL_CURSOR,
    OperationType.CICS_READ,
    OperationType.FILE_READ,
    OperationType.FILE_OPEN,
    OperationType.FILE_CLOSE,
})


@dataclass
class ETLOperation:
    operation_type: OperationType
    raw_statement: str      # first 200 chars of the matched COBOL text
    table_or_file: str      # table, dataset, or file name (upper-cased)
    line_number: int
    is_read: bool           # True = read; False = write / insert / update / delete
    description: str        # plain-English one-liner


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each entry: (OperationType, compiled_pattern)
# Named group "target" captures the table/file name.

_SQL_PATTERNS: list[tuple[OperationType, re.Pattern]] = [
    (
        OperationType.SQL_SELECT,
        re.compile(
            r"EXEC\s+SQL\s+(?:DECLARE\s+\w+\s+CURSOR\s+FOR\s+)?SELECT\b.*?"
            r"FROM\s+(?P<target>[\w.$#@-]+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        OperationType.SQL_INSERT,
        re.compile(
            r"EXEC\s+SQL\s+INSERT\s+INTO\s+(?P<target>[\w.$#@-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.SQL_UPDATE,
        re.compile(
            r"EXEC\s+SQL\s+UPDATE\s+(?P<target>[\w.$#@-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.SQL_DELETE,
        re.compile(
            r"EXEC\s+SQL\s+DELETE\s+FROM\s+(?P<target>[\w.$#@-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.SQL_CURSOR,
        re.compile(
            r"EXEC\s+SQL\s+(?:OPEN|FETCH|CLOSE)\s+(?P<target>[\w.$#@-]+)",
            re.IGNORECASE,
        ),
    ),
]

_CICS_PATTERNS: list[tuple[OperationType, re.Pattern]] = [
    (
        OperationType.CICS_READ,
        re.compile(
            r"EXEC\s+CICS\s+READ\s+(?:DATASET|FILE)\s*\(\s*(?P<target>['\"]?[\w.$#@-]+['\"]?)\s*\)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.CICS_WRITE,
        re.compile(
            r"EXEC\s+CICS\s+WRITE\s+(?:DATASET|FILE)\s*\(\s*(?P<target>['\"]?[\w.$#@-]+['\"]?)\s*\)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.CICS_REWRITE,
        re.compile(
            r"EXEC\s+CICS\s+REWRITE\s+(?:DATASET|FILE)\s*\(\s*(?P<target>['\"]?[\w.$#@-]+['\"]?)\s*\)",
            re.IGNORECASE,
        ),
    ),
    (
        OperationType.CICS_DELETE,
        re.compile(
            r"EXEC\s+CICS\s+DELETE\s+(?:DATASET|FILE)\s*\(\s*(?P<target>['\"]?[\w.$#@-]+['\"]?)\s*\)",
            re.IGNORECASE,
        ),
    ),
]

# Sequential file I/O — COBOL OPEN/READ/WRITE/CLOSE verbs in the Procedure Division.
# These patterns are deliberately conservative (no DOTALL) to avoid false positives.
_FILE_PATTERNS: list[tuple[OperationType, re.Pattern]] = [
    (
        OperationType.FILE_OPEN,
        re.compile(
            r"^\s{0,6}(?!\*)\s*OPEN\s+(?:INPUT|OUTPUT|I-O|EXTEND)\s+(?P<target>[\w-]+)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        OperationType.FILE_READ,
        re.compile(
            r"^\s{0,6}(?!\*)\s*READ\s+(?P<target>[\w-]+)(?:\s+(?:INTO|AT\s+END|NEXT|RECORD))?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        OperationType.FILE_WRITE,
        re.compile(
            r"^\s{0,6}(?!\*)\s*WRITE\s+(?P<target>[\w-]+)(?:-RECORD)?(?:\s+(?:FROM|AT|AFTER|BEFORE))?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        OperationType.FILE_CLOSE,
        re.compile(
            r"^\s{0,6}(?!\*)\s*CLOSE\s+(?P<target>[\w-]+)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
]

_ALL_PATTERNS = _SQL_PATTERNS + _CICS_PATTERNS + _FILE_PATTERNS

_DESCRIPTIONS: dict[OperationType, str] = {
    OperationType.SQL_SELECT:   "Read data from table {t}",
    OperationType.SQL_INSERT:   "Insert record into table {t}",
    OperationType.SQL_UPDATE:   "Update record in table {t}",
    OperationType.SQL_DELETE:   "Delete record from table {t}",
    OperationType.SQL_CURSOR:   "Cursor operation on {t}",
    OperationType.CICS_READ:    "Read record from CICS dataset {t}",
    OperationType.CICS_WRITE:   "Write record to CICS dataset {t}",
    OperationType.CICS_REWRITE: "Update record in CICS dataset {t}",
    OperationType.CICS_DELETE:  "Delete record from CICS dataset {t}",
    OperationType.FILE_OPEN:    "Open file {t}",
    OperationType.FILE_READ:    "Read record from file {t}",
    OperationType.FILE_WRITE:   "Write record to file {t}",
    OperationType.FILE_CLOSE:   "Close file {t}",
}


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class ETLDetector:
    """Scan COBOL source text and return all detected ETL/IO operations."""

    def extract(self, source: str) -> list[ETLOperation]:
        lines = source.splitlines()
        operations: list[ETLOperation] = []

        for op_type, pattern in _ALL_PATTERNS:
            for match in pattern.finditer(source):
                line_no = source[: match.start()].count("\n") + 1
                matched_line = lines[line_no - 1] if line_no <= len(lines) else ""
                if _is_comment_line(matched_line):
                    continue

                raw_target = (match.groupdict().get("target") or "").strip("'\" ")
                if not raw_target:
                    continue

                target = raw_target.upper().rstrip(".")
                operations.append(
                    ETLOperation(
                        operation_type=op_type,
                        raw_statement=match.group(0).strip()[:200],
                        table_or_file=target,
                        line_number=line_no,
                        is_read=op_type in _READ_TYPES,
                        description=_DESCRIPTIONS[op_type].format(t=target),
                    )
                )

        # Stable order by line number; de-duplicate exact (type, line) pairs
        seen: set[tuple[OperationType, int]] = set()
        unique: list[ETLOperation] = []
        for op in sorted(operations, key=lambda o: o.line_number):
            key = (op.operation_type, op.line_number)
            if key not in seen:
                seen.add(key)
                unique.append(op)
        return unique

    def extract_from_file(self, path: Path) -> list[ETLOperation]:
        source = _read_cobol(path)
        return self.extract(source)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_comment_line(line: str) -> bool:
    if len(line) >= 7 and line[6] in ("*", "/", "d", "D"):
        return True
    stripped = line.lstrip()
    return stripped.startswith("*") or stripped.startswith("/")


def _read_cobol(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def etl_operations_summary(ops: list[ETLOperation]) -> str:
    """Return a human-readable summary string for logging or documentation."""
    reads  = [o for o in ops if o.is_read]
    writes = [o for o in ops if not o.is_read]
    lines = [
        f"ETL operations detected: {len(ops)} total "
        f"({len(reads)} read, {len(writes)} write/modify)",
    ]
    if writes:
        lines.append("Write/modify operations:")
        for op in writes:
            lines.append(f"  line {op.line_number:>5}: [{op.operation_type.value}] {op.description}")
    return "\n".join(lines)
