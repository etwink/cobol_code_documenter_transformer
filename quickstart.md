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

# Output goes into a dedicated subfolder inside DOCUMENTS_PATH so it stays
# co-located with the source files.  The pipeline automatically excludes this
# folder from scanning, so re-running will not pick up the generated Python files.
output_dir = config.OUTPUT_PATH

pipeline = TransformationPipeline(
    context_block=""  # optional — see Step 5
)

print(f"Output will be written to: {output_dir}")
print("Running transformation pipeline...")
output = pipeline.run(
    input_paths=config.DOCUMENTS_PATHS or [Path(".")],
    output_dir=output_dir,
    progress_callback=on_progress,
)

writer = OutputWriter(output_dir=output_dir)
report = writer.write(output)

print(f"\n{report}")
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

After the run, a `_cobol_transformer_output/` folder is created **inside your `DOCUMENTS_PATH`**:

```
<DOCUMENTS_PATH>/
└── _cobol_transformer_output/
    ├── python_db/
    │   ├── __init__.py
    │   ├── requirements.txt
    │   ├── quickstart.md
    │   └── <program_name>.py      # Python with real SQLAlchemy database calls
    ├── python_etl/
    │   ├── __init__.py
    │   ├── requirements.txt
    │   ├── quickstart.md
    │   └── <program_name>.py      # Python with pipe-delimited file I/O for ETL
    ├── documentation.md           # Full technical document
    └── etl_operations.csv         # Every detected database/file operation
```

The folder name starts with `_` and is excluded from the scanner automatically — re-running the pipeline will not pick up the generated Python files as source code.

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

**`openai.APIConnectionError: Connection error.`**

This error means the OpenAI SDK could not establish a TCP connection to Azure at all. Two things cause it: wrong connection details in `.env`, or an SSL/TLS certificate problem on your machine (common in corporate networks with a proxy or custom CA).

**Step 1 — Rule out a bad `.env` config first**

Check these three things before touching SSL:

- Open `.env` and confirm `AZURE_OPENAI_ENDPOINT` ends with a trailing slash and uses `https://`, e.g. `https://my-resource.openai.azure.com/`. A missing slash or `http://` will cause a connection error.
- Confirm `AZURE_OPENAI_DEPLOYMENT_NAME` matches the deployment name exactly as it appears in the Azure portal (case-sensitive).
- Run a quick connectivity test from the terminal — if this returns an error, the endpoint or key is wrong; if it returns JSON, your config is fine and the problem is SSL:

```bash
curl -s -o /dev/null -w "%{http_code}" \
  "https://<your-resource>.openai.azure.com/openai/deployments?api-version=2024-02-15-preview" \
  -H "api-key: <your-key>"
```

A `200` or `401` response means the host is reachable. A `000` or `SSL` error means the certificate chain is broken on your machine.

**Step 2 — Fix SSL certificate issues with `truststore`**

Corporate networks often intercept HTTPS traffic using a private CA certificate that Python does not trust by default. `truststore` fixes this by injecting your OS certificate store (which already trusts the corporate CA) into Python's SSL context.

Install it:

```bash
pip install truststore
```

Add these two lines to the very top of `run.py`, before any other imports:

```python
import truststore
truststore.inject_into_ssl()
```

Your `run.py` should start like this:

```python
import truststore
truststore.inject_into_ssl()

import sys
sys.path.insert(0, r"C:\path\to\cobol_code_documenter_transformer")

from pathlib import Path
from transformation_pipeline import TransformationPipeline
from output_writer import OutputWriter
# ... rest of your script
```

`truststore` must be called before the `openai` package initializes its HTTP client, so it must appear before any project imports.

**Still failing after both steps?**

If you still get a connection error after adding `truststore`, check whether a network proxy is required. Some corporate environments block direct HTTPS and require an explicit proxy. Set it in your terminal before running:

```bash
set HTTPS_PROXY=http://proxy.yourcompany.com:8080   # Windows
```

Or add it permanently to your `.env`:

```ini
HTTPS_PROXY=http://proxy.yourcompany.com:8080
```

---

**`etl_operations.csv` is empty but the COBOL clearly has SQL**

The static ETL detector looks for standard patterns (`EXEC SQL`, `EXEC CICS`, `OPEN/READ/WRITE/CLOSE`). Check:
- The file extension is in the supported COBOL list (`.CIC`, `.CPY`, `.MPS`, etc.)
- The SQL statements follow standard COBOL/DB2 syntax
- Lines are not all in the comment area (column 7 = `*`)

The LLM-generated Python code may still contain database operations even when the detector found none — the LLM infers from context.
