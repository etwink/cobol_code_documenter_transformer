# COBOL Code Documenter & Transformer

Transforms legacy COBOL programs into Python code and produces comprehensive technical documentation. For each COBOL program it generates **two Python versions** and a detailed document that explains the system to readers who have never seen the code before.

---

## What It Does

1. **Scans** a folder of COBOL source files and supporting documents (Word, Excel, other code).
2. **Analyzes** COBOL dependencies (COPY, CALL, EXEC CICS LINK) and groups related files into logical subsystem clusters.
3. **Transforms** each COBOL file into Python — twice:
   - **DB Version** — database operations (SQL, CICS) kept as real SQLAlchemy / pyodbc calls, identical in behavior to the original COBOL.
   - **ETL/File Version** — all write/insert/update/delete operations replaced with CSV staging files for a downstream ETL job; read-only queries remain as live database calls.
4. **Documents** the entire system in a single Markdown file covering overview, assumptions, ETL interaction steps, architecture, data flow, business logic, and a full appendix.

---

## Key Concepts

### Two Python Versions

| | DB Version (`python_db/`) | ETL/File Version (`python_etl/`) |
|---|---|---|
| **Database reads** | Live SQL queries | Live SQL queries (same) |
| **Database writes** | `INSERT / UPDATE / DELETE` via SQLAlchemy | Writes to `etl_stage_<table>.csv` staging files |
| **Marker comment** | `# DB_OPERATION: ...` | `# ETL_STEP: ...` (writes) / `# DB_OPERATION: ...` (reads) |
| **Business logic** | Identical | Identical |
| **Use case** | Direct migration, full DB access | Environments where writes must go through an ETL pipeline |

### Assumptions Section

The COBOL source often does not show what triggers a program, what ran before it, or what consumes its output. The documentation includes an **Assumptions** section that records what was inferred from the code, the supporting evidence, and a confidence level (HIGH / MEDIUM / LOW). This section must be validated with the original system owners before production use.

### ETL Interaction Steps

Every write operation found in the source (EXEC SQL INSERT/UPDATE/DELETE, EXEC CICS WRITE/REWRITE/DELETE) is listed in the **ETL Interaction Steps** section of the documentation. This section is written for the ETL engineering team and maps each COBOL operation to its staging file, its target table, and the action the downstream ETL job must perform.

---

## Project Structure

```
cobol_code_documenter_transformer/
│
├── etl_detector.py             # Regex-based COBOL SQL/file I/O scanner
├── cobol_transformer.py        # LLM-driven COBOL → Python transformer
├── transformation_pipeline.py  # Orchestrator: scan → cluster → transform → document
├── output_writer.py            # Writes Python files + documentation to disk
│
├── folder_scanner.py           # File type scanner (COBOL, Word, Excel, code)
├── cobol_dependency_analyzer.py # COPY/CALL/CICS dependency graph builder
├── cluster_builder.py          # Groups files into logical subsystem clusters
├── hierarchical_summarizer.py  # Produces per-cluster LLM summaries
│
├── analyzers/
│   └── document_analyzer.py    # Document analysis and process document builder
├── document_loaders/
│   ├── base.py                 # BaseDocumentLoader, DocumentContent
│   └── loaders.py              # Loaders for COBOL, Word, Excel, HTML, text
├── llm_integration/
│   ├── azure_client.py         # AzureLLMClient + PromptBuilder
│   └── llm_logger.py           # Logs every LLM call to logs/llm_calls.log
├── utils/
│   └── file_utils.py           # File validation helpers
│
├── config.py                   # Loads .env and exposes config constants
├── .env.example                # Template for required environment variables
└── logs/
    └── llm_calls.log           # Auto-created; one entry per LLM call
```

---

## Output Structure

After a successful run the `output/` directory (or the directory you specify) contains:

```
output/
├── python_db/
│   ├── program_a.py            # DB version of PROGRAM_A.CIC
│   └── program_b.py
├── python_etl/
│   ├── program_a.py            # ETL/file version of PROGRAM_A.CIC
│   └── program_b.py
├── documentation.md            # Full technical documentation (Markdown)
└── etl_operations.csv          # Flat inventory of every detected ETL operation
```

---

## Supported File Types

| Category | Extensions |
|---|---|
| COBOL source | `.COB` `.CIC` `.CPY` `.MPS` `.SRC` `.CT1` `.JCV` `.PRV` `.CBL` `.COBOL` |
| Other source code | `.PY` `.SQL` `.JS` `.TS` `.VB` `.BAS` `.CS` `.JAVA` `.PS1` `.R` `.SH` `.BAT` `.CMD` |
| Word documents | `.DOCX` `.DOC` |
| Excel workbooks | `.XLSX` `.XLS` `.XLSM` `.XLSB` |

Word and Excel files are treated as supporting business documentation. The system attempts to match them to the relevant COBOL subsystem cluster by scanning for program name mentions; unmatched documents are grouped separately.

---

## Prerequisites

- Python 3.11 or later
- An Azure OpenAI resource with a reasoning model deployed (e.g. `o3`, `o4-mini`)
- Access to the COBOL source files (plain text; mainframe EBCDIC exports should be converted to ASCII/Latin-1 first)

### Python Dependencies

```
openai
python-dotenv
python-docx
openpyxl
pandas
sqlalchemy
pyxlsb      # optional — required for .xlsb binary Excel files
oletools    # optional — required for VBA macro extraction from .xlsm/.xlsb
```

Install all at once:

```bash
pip install openai python-dotenv python-docx openpyxl pandas sqlalchemy
pip install pyxlsb oletools  # optional
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your Azure OpenAI details:

```ini
# Required
AZURE_OPENAI_API_KEY=your-api-key-here
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=your-deployment-name
AZURE_OPENAI_API_VERSION=2024-02-15-preview

# Paths — comma-separated if multiple folders
DOCUMENTS_PATH=C:/path/to/cobol/files

# Model behaviour
MODEL_REASONING_EFFORT=medium   # low | medium | high
MODEL_MAX_TOKENS=4000
```

`MODEL_REASONING_EFFORT` controls the thinking budget used by Azure reasoning models. Use `high` for complex programs; `low` is faster but may miss subtle logic.

---

## Running the Pipeline

### Minimal script

```python
from pathlib import Path
from transformation_pipeline import TransformationPipeline
from output_writer import OutputWriter

pipeline = TransformationPipeline(
    context_block=(
        "These programs belong to the nightly claims processing batch. "
        "They run on a mainframe and interact with DB2 tables."
    )
)

output = pipeline.run(["C:/cobol/source", "C:/cobol/docs"])

writer = OutputWriter(output_dir=Path("output"))
writer.write(output)

print(f"Done. Files written to: output/")
```

### With progress reporting

```python
def on_progress(stage: str, current: int, total: int) -> None:
    print(f"[{current}/{total}] {stage}")

output = pipeline.run(
    input_paths=["C:/cobol/source"],
    progress_callback=on_progress,
)
```

### context_block

The `context_block` is free-text that gets injected into every LLM prompt. Use it to describe what you know about the system upfront — the business domain, the platform, known triggers, etc. The more context you provide here, the more accurate the documentation and assumptions sections will be. Leave it empty if you have no prior knowledge.

---

## Logging

Every LLM call is appended to `logs/llm_calls.log` automatically. The log includes the full prompt, the response, the model name, and a timestamp. This is useful for auditing, debugging, and estimating token costs.

To limit log verbosity, set `LLM_LOG_MAX_CHARS=5000` in your `.env`.

---

## Limitations

- **Source truncation** — COBOL files longer than ~14,000 characters are truncated before being sent to the LLM. The ETL detector (regex-based) always processes the full file. Truncated files are flagged in the documentation and the transformation notes.
- **Dynamic CALL targets** — COBOL programs that use variable identifiers in CALL statements (e.g. `CALL WS-PROG-NAME`) cannot be resolved statically; they appear as dynamic references in the dependency graph.
- **EBCDIC encoding** — Source files exported from a mainframe may use Latin-1 encoding. The loaders fall back to Latin-1 automatically, but EBCDIC binary files must be converted before use.
- **ETL detection accuracy** — The regex detector covers common patterns (EXEC SQL, EXEC CICS, sequential file verbs). Unusual coding styles or inline comments may cause misses or false positives. Review `etl_operations.csv` after each run.
- **Assumptions require validation** — The Assumptions section is the LLM's best guess. Always review it with someone who knows the original system before using the documentation as an authoritative source.
