"""Scan one or more directories and categorize files by type."""

import os
from pathlib import Path
from dataclasses import dataclass, field


COBOL_EXTENSIONS = {".COB", ".CIC", ".CPY", ".MPS", ".SRC", ".CT1", ".JCV", ".PRV", ".CBL", ".COBOL"}

# Non-COBOL source code — analyzed as text, LLM-clustered (no dependency graph)
CODE_EXTENSIONS = {
    ".PY",   # Python
    ".SQL",  # SQL / T-SQL / PL-SQL / stored procedures
    ".JS",   # JavaScript
    ".TS",   # TypeScript
    ".VB",   # Visual Basic
    ".BAS",  # BASIC / VBA module exports
    ".CS",   # C#
    ".JAVA", # Java
    ".PS1",  # PowerShell
    ".R",    # R
    ".SH",   # Shell script
    ".BAT",  # Windows batch
    ".CMD",  # Windows command
}

WORD_EXTENSIONS  = {".DOCX", ".DOC"}
EXCEL_EXTENSIONS = {".XLSX", ".XLS", ".XLSM", ".XLSB"}

# Plain-text documents (change logs, procedures, readme files, data extracts, etc.)
OTHER_EXTENSIONS = {".TXT"}


@dataclass
class ScannedDocuments:
    cobol: list[Path] = field(default_factory=list)
    code:  list[Path] = field(default_factory=list)   # Non-COBOL source code
    word:  list[Path] = field(default_factory=list)
    excel: list[Path] = field(default_factory=list)
    other: list[Path] = field(default_factory=list)   # Plain-text docs (.txt)

    @property
    def total_count(self) -> int:
        return len(self.cobol) + len(self.code) + len(self.word) + len(self.excel) + len(self.other)

    def summary(self) -> str:
        parts = []
        if self.cobol:
            parts.append(f"{len(self.cobol)} COBOL")
        if self.code:
            parts.append(f"{len(self.code)} source code")
        if self.word:
            parts.append(f"{len(self.word)} Word")
        if self.excel:
            parts.append(f"{len(self.excel)} Excel")
        if self.other:
            parts.append(f"{len(self.other)} text")
        if not parts:
            return "0 files found"
        return ", ".join(parts) + f" ({self.total_count} total)"


class FolderScanner:
    """Scan one or more directories for supported file types."""

    def scan(self, paths: list[Path | str], recursive: bool = True) -> ScannedDocuments:
        result = ScannedDocuments()
        seen: set[Path] = set()

        for root in paths:
            root = Path(root)
            if not root.exists():
                continue

            if recursive:
                # os.walk() is reliable across all Python versions and OS platforms;
                # Path.glob("**/*") has known issues on Windows with some directory structures.
                for dirpath, _, filenames in os.walk(root):
                    for filename in sorted(filenames):
                        self._categorize(Path(dirpath) / filename, result, seen)
            else:
                for p in sorted(root.iterdir()):
                    if p.is_file():
                        self._categorize(p, result, seen)

        return result

    @staticmethod
    def _categorize(p: Path, result: ScannedDocuments, seen: set[Path]) -> None:
        resolved = p.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        ext = p.suffix.upper()
        if ext in COBOL_EXTENSIONS:
            result.cobol.append(p)
        elif ext in CODE_EXTENSIONS:
            result.code.append(p)
        elif ext in WORD_EXTENSIONS:
            result.word.append(p)
        elif ext in EXCEL_EXTENSIONS:
            result.excel.append(p)
        elif ext in OTHER_EXTENSIONS:
            result.other.append(p)
