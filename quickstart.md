# Quickstart Guide

Get from zero to a working transformation in five steps.

---

## Step 1 — Install dependencies

```bash
pip install openai python-dotenv python-docx openpyxl pandas sqlalchemy
```

Optional (needed for `.xlsb` and `.xlsm` VBA macro extraction):

```bash
pip install pyxlsb oletools
```

Requires **Python 3.11+**. Check your version:

```bash
python --version
```

---

## Step 2 — Configure Azure OpenAI

Copy the example env file:

```bash
cp .env.example .env
```

Open `.env` and fill in the four required fields:

```ini
AZURE_OPENAI_API_KEY=<your key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=<your deployment, e.g. o4-mini>
AZURE_OPENAI_API_VERSION=2024-02-15-preview
```

Leave everything else at its default for now.

---

## Step 3 — Point at your COBOL files

Set `DOCUMENTS_PATH` in `.env` to the folder containing your COBOL source:

```ini
DOCUMENTS_PATH=C:/path/to/cobol/folder
```

You can point at multiple folders (comma-separated):

```ini
DOCUMENTS_PATH=C:/cobol/source,C:/cobol/copybooks,C:/cobol/procedures
```

Word and Excel files in the same folders are automatically picked up as supporting documentation.

---

## Step 4 — Run the pipeline

Create a file called `run.py` in the project root:

```python
from pathlib import Path
from transformation_pipeline import TransformationPipeline
from output_writer import OutputWriter
import config

def on_progress(stage: str, current: int, total: int) -> None:
    print(f"  [{current}/{total}] {stage}")

pipeline = TransformationPipeline(
    context_block=""  # optional — see Step 5
)

print("Running transformation pipeline...")
output = pipeline.run(
    input_paths=config.DOCUMENTS_PATHS or [Path(".")],
    progress_callback=on_progress,
)

writer = OutputWriter(output_dir=Path("output"))
path = writer.write(output)

print(f"\nDone. Output written to: {path.resolve()}")
```

Run it:

```bash
python run.py
```

---

## Step 5 — Provide business context (recommended)

The `context_block` is the single most effective way to improve documentation quality. Pass anything you know about the system:

```python
pipeline = TransformationPipeline(
    context_block="""
    These programs are part of the nightly claims adjudication batch.
    They run on an IBM z/OS mainframe and interact with DB2 tables in the CLAIMS schema.
    The batch is triggered by the job scheduler at 11 PM EST after the day's transactions close.
    Output is consumed by the morning reporting team and the downstream payment system.
    """
)
```

If you have no prior knowledge, leave it as an empty string. The Assumptions section in the documentation will then be longer and more tentative, which is expected.

---

## What You Get

After the run, the `output/` folder contains:

```
output/
├── python_db/
│   └── <program_name>.py      # Python with real SQLAlchemy database calls
├── python_etl/
│   └── <program_name>.py      # Python that stages writes to CSV files
├── documentation.md           # Full technical document
└── etl_operations.csv         # Every detected database/file operation
```

Open `documentation.md` in any Markdown viewer (VS Code, GitHub, Obsidian) to read the full document. The key sections to review first:

1. **Assumptions** — what the LLM inferred about the process; validate with system owners
2. **ETL Interaction Steps** — hand this to the ETL team; lists every staging file and what the ETL job must do
3. **Overview** — high-level summary of what was transformed and how

---

## Inspecting the Generated Python

### Finding database operations

Both Python versions annotate every database call. Search for `# DB_OPERATION:` to jump to any query:

```python
# DB_OPERATION: Fetch policy record by policy number and effective date
row = session.execute(
    select(Policy).where(
        Policy.policy_number == policy_number,
        Policy.effective_date <= process_date,
    )
).scalar_one_or_none()
```

### Finding ETL staging steps (ETL version only)

Search for `# ETL_STEP:` to find every place where data is written to a staging file:

```python
# ETL_STEP: Stage claim header insert into etl_stage_claim_header.csv
staging_rows["claim_header"].append({
    "claim_id":   claim_id,
    "policy_id":  policy_id,
    "claim_date": claim_date,
    "amount":     amount,
})
```

### The ETL contract block

Every ETL-version module opens with a contract comment listing all staging files it produces:

```python
# ── ETL CONTRACT ─────────────────────────────────────────────────────────────
# This module produces the following staging files for downstream ETL processing:
#
#   etl_stage_claim_header.csv     ← INSERT  → CLAIMS.CLAIM_HEADER
#   etl_stage_claim_line.csv       ← INSERT  → CLAIMS.CLAIM_LINE
#   etl_stage_policy_audit.csv     ← UPDATE  → CLAIMS.POLICY_AUDIT
#
# Each staging file uses UTF-8 CSV with a header row.
# The downstream ETL job must apply these files in the order listed.
# ─────────────────────────────────────────────────────────────────────────────
```

---

## Common Issues

**`ModuleNotFoundError: No module named 'llm_integration'`**

Run the script from the project root directory, not from a subdirectory:

```bash
cd "C:/path/to/cobol_code_documenter_transformer"
python run.py
```

Or add the project root to `sys.path` at the top of `run.py`:

```python
import sys
sys.path.insert(0, r"C:\path\to\cobol_code_documenter_transformer")
```

---

**`ValueError: AZURE_OPENAI_API_KEY not set in .env`**

The `.env` file is missing or in the wrong location. It must be in the project root directory alongside `config.py`.

---

**Source files are truncated in the output**

COBOL files longer than ~14,000 characters are truncated before being sent to the LLM to stay within context limits. The ETL detector (regex-based) always processes the full file. You will see this flagged in the transformation notes table in `documentation.md`.

For very large programs, consider splitting the source file or increasing `MODEL_MAX_TOKENS` in `.env`.

---

**`etl_operations.csv` is empty but the COBOL clearly has SQL**

The static ETL detector looks for standard patterns (`EXEC SQL`, `EXEC CICS`, `OPEN/READ/WRITE/CLOSE`). Check:
- The file extension is in the supported COBOL list (`.CIC`, `.CPY`, `.MPS`, etc.)
- The SQL statements follow standard COBOL/DB2 syntax
- Lines are not all in the comment area (column 7 = `*`)

The LLM-generated Python code may still contain database operations even when the detector found none — the LLM infers from context.
