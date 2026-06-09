# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — 2026-06-09 · `8645712`

### Fixed — Duplicate ETL entries for same cursor/file
- `etl_detector.py`: new `_deduplicate()` method runs three passes after extraction:
  1. Drop `FILE_OPEN` and `FILE_CLOSE` when `FILE_READ` or `FILE_WRITE` exists for the same file — they are pure infrastructure with no ETL value
  2. For `SQL_CURSOR` (`OPEN`/`FETCH`/`CLOSE`), keep only the first per cursor name — the `DECLARE CURSOR` is already captured as `SQL_SELECT` with the real table name; the lifecycle ops were generating spurious duplicate input-file entries
  3. For each `(table_or_file, is_read)` pair, keep only the highest-priority operation type using the new `_OP_PRIORITY` ranking (`SQL_SELECT=10` > `FILE_READ=8` > `SQL_CURSOR=5` > `FILE_OPEN/CLOSE=1`)

### Improved — ETL quickstart format
- `output_writer.py`: each unique ETL file now appears exactly once in the "Before Running" and "After Running" sections; the originating COBOL `EXEC SQL` statement is shown inline beneath the filename for SQL operations so the ETL engineer can see exactly what data is expected
- `output_writer.py`: ETL job specifications deduplicated to one spec per unique file
- Added `_unique_by_filename()`, `_format_etl_file_entry()`, and `_sql_hint()` helpers

---

## [1.2.0] — 2026-06-09 · `66c2c46`

### Fixed — ETL Python files contained error comments instead of code
- `cobol_transformer.py`: `*common_args` expansion placed `system_context` (a `str`) into the `etl_ops` parameter of `_generate_etl_version` and the actual `etl_ops` list into `sys_ctx`; iterating over the string characters and calling `.is_read` raised `AttributeError: 'str' object has no attribute 'is_read'`; `_call_llm` caught this and wrote the error as a Python comment, so no ETL code was ever generated — fixed by expanding args explicitly in the correct order; the chunked path was unaffected as it uses `etl_ops=etl_ops` keyword argument

---

## [1.1.0] — 2026-06-05 · `175e2b9`

### Fixed — Quickstart shows wrong entry point module
- `output_writer.py`: replaced fragile `"__main__" in generated_code` string search with the authoritative `ClusterSummary.entry_point` value — the program that is never called by any other program in the COBOL dependency graph; the LLM may or may not emit `if __name__ == "__main__":` so string-searching generated code is unreliable
- `output_writer.py`: `entry_point` is now plumbed through `write()` → `_write_db/etl_package_files()` → `_build_db/etl_quickstart()` so both quickstart files always use the correct module; falls back to the first module alphabetically when no entry point exists (e.g. all files are copybooks/utilities)

### Fixed — Assumptions section always empty
- `cobol_transformer.py`: `_call_assumptions` was called with `*common_args[:2]` (only `filename` and `dep_ctx`) but `_extract_assumptions` requires four arguments; the `except` clause silently swallowed the `TypeError` and returned `[]`; fixed to `*common_args[:3]` to include `doc_ctx`

---

## [1.0.0] — 2026-06-05 · `8007f77`

### Fixed — Generated Python output truncated mid-file
- Root cause: gpt-5-mini has 128k output tokens shared with reasoning; `MODEL_REASONING_EFFORT=medium` consumes ~20-30k reasoning tokens, leaving only ~5-7k tokens for actual code output at the old 10k limit — not enough for a large converted module
- `cobol_transformer.py`: raised all code-generation calls (DB version, ETL version, chunk synthesis) from `max_tokens=10,000` to `max_tokens=32,000` — 32k tokens ≈ 128k chars of Python output, enough for any single module
- `transformation_pipeline.py`: raised `_TOKENS_PER_SECTION` from 6,000 to 12,000 so documentation sections are not cut off mid-paragraph
- `hierarchical_summarizer.py`: raised synthesis calls from 4,000/5,000 to 8,000 so COBOL cluster summaries are complete
- `config.py` / `.env.example`: raised `MODEL_MAX_TOKENS` default from 4,000 to 8,000; added explanation of reasoning token sharing

---

## [0.9.0] — 2026-06-05 · `09782a9`

### Fixed — Large COBOL files truncated in generated output
- `cobol_transformer.py`: raised `_MAX_SOURCE_CHARS` from 14,000 to 150,000 — gpt-5-mini supports 272k input tokens (~1.09M chars); the old limit caused unnecessary `# TODO: truncated` warnings for typical large programs (e.g. the 35,230-char file that triggered this fix)

### Added — Chunked transformation for very large files (> 150k chars)
- `cobol_transformer.py`: `_split_into_chunks()` — finds the PROCEDURE DIVISION, keeps the full header block (IDENTIFICATION, ENVIRONMENT, DATA divisions) in every chunk so data definitions are always in scope, then splits the PROCEDURE DIVISION at paragraph/section boundaries with 3,000-char overlap; falls back to character-based splitting if no boundaries found
- `cobol_transformer.py`: `_transform_chunked()` — calls DB or ETL generation on each chunk independently, then synthesizes results
- `cobol_transformer.py`: `_synthesize_chunks()` — merges chunk-generated Python by deduplicating imports and helpers while preserving all business logic and every `# DB_OPERATION:` / `# ETL_INPUT:` / `# ETL_OUTPUT:` comment
- `cobol_transformer.py`: transformation notes now distinguish between chunked, truncated, and normal processing
- `hierarchical_summarizer.py`: individual file summary content limit raised from 6,000 to 20,000 chars so cluster summaries for large COBOL files see meaningful content beyond the opening declarations

---

## [0.8.0] — 2026-06-05 · `e109af1`

### Changed — Simplified pipeline entry point
- `transformation_pipeline.py`: `run()` now takes `input_path: str | Path` (singular) instead of `input_paths: list[str | Path]` — the pipeline always processes one directory; the list form was misleading and caused confusion between `config.DOCUMENTS_PATH` (string) and `config.DOCUMENTS_PATHS` (list)
- `quickstart.md` / `README.md`: updated all examples to use `input_path=config.DOCUMENTS_PATH`

---

## [0.7.0] — 2026-06-05 · `d99386d`

### Changed — Output directory location
- `config.py`: new `OUTPUT_DIR_NAME = "_cobol_transformer_output"` and `OUTPUT_PATH` — the output folder is now written inside the first `DOCUMENTS_PATH` entry so it stays co-located with the source files; falls back to a local `_cobol_transformer_output/` folder if `DOCUMENTS_PATH` is not set
- `folder_scanner.py`: new `exclude_paths` parameter on `scan()` — a set of resolved absolute paths to skip entirely; matched directories are pruned from `os.walk()` before descent so re-running the pipeline never picks up generated `.py` files as source code
- `transformation_pipeline.py`: new `output_dir` parameter on `run()`; when provided, its resolved path is automatically added to the scanner exclusion set
- `quickstart.md`: `run.py` example updated to use `config.OUTPUT_PATH`; "What You Get" section updated to show the new folder structure
- `.env.example`: added comment on `DOCUMENTS_PATH` explaining where output is written

---

## [0.6.0] — 2026-06-04 · `66b24e2`

### Changed — Connected system architecture
- `cobol_transformer.py`: new `system_context` parameter on `transform()` — a map of every program in the scan and its Python module name; injected into DB and ETL version prompts so the LLM generates correct relative imports (`from . import <module>`) for CALL/COPY dependencies
- `transformation_pipeline.py`: new `_build_system_map()` helper builds a human-readable system map (program name, Python module name, calls, called-by) passed to every transformer call
- `cobol_transformer.py` DB version prompt: instructs the LLM to expose a clear entry point or public function, use package-style imports, and avoid duplicating logic across modules

### Changed — ETL version redesign (fully file-based, no DB connections)
- `cobol_transformer.py` ETL version prompt: completely rewritten — the ETL version now has **zero database connections**; all DB reads become reads from ETL-provided pipe-delimited input files (`etl_in_<TABLE>.txt`); all DB writes become pipe-delimited output files (`etl_out_<TABLE>.txt`) for the ETL environment to process
- File format: pipe-delimited (`|`) UTF-8 `.txt` files with a header row — not CSV
- ETL contract block now documents both input and output files, including ETL job specifications (what query the ETL job must run to produce input files; what table operation it must perform on output files)
- `transformation_pipeline.py`: all `etl_stage_` references replaced with `etl_out_`

### Added — Per-version package files
- `output_writer.py`: writes `python_db/__init__.py` and `python_etl/__init__.py` making each output directory a proper Python package
- `output_writer.py`: writes `python_db/requirements.txt` (SQLAlchemy + dotenv) and `python_etl/requirements.txt` (dotenv only — no DB driver needed)
- `output_writer.py`: writes `python_db/quickstart.md` — DB connection config, how to run, module list, code navigation
- `output_writer.py`: writes `python_etl/quickstart.md` — how the file-based ETL pattern works, input files required before running, output files produced after running, per-file ETL job specifications for the ETL engineering team

---

## [0.5.0] — 2026-06-04 · `e457dcb`

### Fixed — Recursive folder scanning not entering subdirectories
- `folder_scanner.py`: replaced `Path.glob("**/*")` with `os.walk()` for recursive mode — `glob` has known reliability issues on Windows with certain directory structures; `os.walk()` is guaranteed to traverse all subdirectories on every platform
- `folder_scanner.py`: switched non-recursive mode from `glob("*")` to `Path.iterdir()` for consistency; extracted categorization into a `_categorize()` static method; deduplication now uses resolved paths to handle symlinks correctly

### Added — `.txt` file support
- `folder_scanner.py`: new `OTHER_EXTENSIONS = {".TXT"}` constant and `other: list[Path]` field on `ScannedDocuments`; `summary()` reports text file counts
- `transformation_pipeline.py`: `scanned.other` merged into `word_files` when calling `build_clusters()` so `.txt` files are treated as documentation and matched to COBOL clusters by program-name mentions
- `cluster_builder.py`: `.TXT` added to `word_exts` in `_fallback_cluster_by_type` so `.txt` files group with business documents when LLM clustering falls back
- `document_loaders/loaders.py`: `.txt` was already mapped to `TextDocumentLoader` — no change needed

---

## [0.4.0] — 2026-06-04 · `c9dcbb8`

### Fixed — Empty transformations / no Python files written
- `transformation_pipeline.py`: raise `ValueError` immediately after scanning when no COBOL files are found, with a clear message showing the paths searched, the supported extensions, and what other files were discovered — instead of silently completing with empty output
- `transformation_pipeline.py`: emit a scan-complete progress message after scanning so file counts are visible before any LLM calls are made
- `transformation_pipeline.py`: resolve input paths before scanning so relative paths appear as absolute paths in error messages
- `transformation_pipeline.py`: wrap each `transformer.transform()` call in `try/except` so an LLM failure on one file no longer blocks all remaining files; failed files produce a `# ERROR:` comment placeholder and are still written to disk
- `cobol_transformer.py`: introduce `_call_llm()` and `_call_assumptions()` helpers so DB version, ETL version, and assumptions extraction each fail independently rather than crashing the whole transform
- `folder_scanner.py`: `summary()` returns `"0 files found"` instead of `" (0 total)"` for the empty case

---

## [0.3.0] — 2026-06-04 · `de6b104`

### Fixed — `output_writer.py`
- **Folders created but no files written**: added per-file try/except around every write operation so a single failure no longer silently stops the rest from writing
- **`_callout` broken Markdown blockquote**: `textwrap.fill` wraps long text onto multiple lines but only the first line had the `> ` prefix; all lines are now correctly prefixed
- **Cross-platform path extraction**: replaced `Path(source_file).stem` with `_safe_stem()`, which splits on both `\\` and `/` before stripping the extension — safe when a path was generated on a different OS
- **OS-default newlines**: `write_text` was using platform newlines (CRLF on Windows); all writes now explicitly pass `newline="\n"` for consistent output across environments
- **Empty LLM output**: a zero-byte `.py` file was written with no indication of failure; empty output now produces a `# WARNING:` placeholder pointing to `logs/llm_calls.log`
- **`documentation.md` silently skipped when `None`**: the guard `if output.documentation:` meant no markdown was written when the documentation builder failed; it now writes a warning stub instead
- **TOC anchor generation**: special characters in heading titles were not fully stripped, producing invalid Markdown anchors; anchor strings are now cleaned with a regex
- **`write()` return type changed from `Path` to `str`**: returns a plain-text report listing every file written (`✓`) and every failure (`✗`), making run output immediately useful for debugging

### Added — `quickstart.md`
- New **Common Issues** entry for `openai.APIConnectionError: Connection error.` covering:
  - How to distinguish a bad `.env` config from an SSL certificate problem (curl connectivity test)
  - Fix using `truststore.inject_into_ssl()` for corporate networks with private CA certificates
  - Proxy configuration fallback for environments that block direct HTTPS

### Added — `requirements.txt`
- Optional `truststore>=0.9.0` entry with inline usage instructions

---

## [0.2.0] — 2026-06-03 · `464a426`

### Fixed — Import errors
- `hierarchical_summarizer.py`: `from .cluster_builder import DocumentCluster` changed to `from cluster_builder import DocumentCluster` — relative import failed when the module is loaded as a top-level module rather than part of a package
- `hierarchical_summarizer.py`: two `from src.llm_integration` and `from src.document_loaders` imports corrected to `from llm_integration` and `from document_loaders`
- `cluster_builder.py`: `from src.llm_integration import AzureLLMClient` corrected to `from llm_integration import AzureLLMClient`
- `analyzers/document_analyzer.py`: both `from src.document_loaders` and `from src.llm_integration` imports corrected

### Changed — Dual-audience documentation
- All section prompts in `_DocumentationBuilder` (`transformation_pipeline.py`) now produce output structured for **two audiences**: product owners (business need, rules, outcomes) and developers (technical implementation, libraries, code patterns)
- A `_DUAL_AUDIENCE` class constant defines the consistent `Business:` / `Technical:` two-line format injected into every section prompt
- `_decision_points`, `_systems`, and `_appendix` section writers converted from delegating to `PromptBuilder` to inline prompts so the dual-audience instruction can be applied
- `_appendix` now includes a dedicated **Roles and Responsibilities** sub-section covering both business roles (product owner, BA) and technical roles (developer, ETL engineer, DBA)
- `hierarchical_summarizer.py`: both `_synthesize_cobol` and `_synthesize_mixed` prompts updated with the `Business:` / `Technical:` format so cluster summaries (which feed documentation sections) already carry dual-audience content
- `README.md`: added **Dual-Audience Documentation** section describing the two target audiences and the format used throughout the document

---

## [0.1.0] — 2026-06-02 · `0a9ae11`

### Added — Core transformation pipeline
- `etl_detector.py`: regex-based scanner that identifies EXEC SQL (SELECT, INSERT, UPDATE, DELETE, cursor), EXEC CICS (READ, WRITE, REWRITE, DELETE), and sequential file I/O (OPEN, READ, WRITE, CLOSE) in COBOL source; returns typed `ETLOperation` objects with line number, target table/file, and read/write flag
- `cobol_transformer.py`: LLM-driven transformer producing two Python variants per COBOL file:
  - **DB version** — real SQLAlchemy/pyodbc database calls; every call annotated with `# DB_OPERATION:`
  - **ETL/file version** — write/modify operations replaced with CSV staging files; every staged write annotated with `# ETL_STEP:`; module-level `# ── ETL CONTRACT ──` block lists all staging files produced
  - Assumptions extractor: six structured inferences (trigger, pre-conditions, outputs, business process, boundaries, missing context) with evidence and confidence levels for the Assumptions section
- `transformation_pipeline.py`: orchestrator running the full pipeline — scan → cluster → summarize → transform → document; `TransformationPipeline.run()` accepts input paths and an optional `context_block` description; produces `TransformationOutput` containing transformations, documentation, ETL operations, and cluster summaries
- `output_writer.py`: writes `python_db/`, `python_etl/`, `documentation.md`, and `etl_operations.csv` to an output directory
- `transformation_pipeline.py` — `_DocumentationBuilder`: generates eleven documentation sections from cluster summaries and transformation results: Overview, Assumptions, ETL Interaction Steps, Python DB Architecture, Python ETL Architecture, Database Operations, Business Logic, Data Flow, Decision Points, Systems and Components, Appendix

### Added — Documentation
- `README.md`: project overview, key concepts (two Python versions, Assumptions section, ETL Interaction Steps), project file structure, output layout, supported file types, prerequisites, configuration reference, usage examples, known limitations
- `quickstart.md`: five-step getting-started guide with a ready-to-copy `run.py`, guide to navigating generated Python via comment markers, and common issues section
- `requirements.txt`: pinned dependencies split into core (required to run pipeline), optional document loaders, and required-to-run-generated-code sections

### Added — Infrastructure (carried from prior project)
- `folder_scanner.py`: file type scanner categorising COBOL, source code, Word, and Excel files
- `cobol_dependency_analyzer.py`: builds COPY/CALL/EXEC CICS LINK dependency graph with CSV and Graphviz DOT export
- `cluster_builder.py`: groups files into logical subsystem clusters using the dependency graph and LLM-based doc clustering
- `hierarchical_summarizer.py`: produces per-cluster LLM summaries (COBOL-only, mixed COBOL+doc, and pure-doc strategies)
- `analyzers/document_analyzer.py`: `DocumentAnalyzer`, `ProcessDocumentBuilder`, `GapAnalyzer`, `ClarificationQuestionGenerator`
- `llm_integration/azure_client.py`: `AzureLLMClient` (Azure OpenAI Responses API) and `PromptBuilder` with per-section prompts and audience instructions
- `llm_integration/llm_logger.py`: appends every LLM request and response to `logs/llm_calls.log`
- `document_loaders/loaders.py`: loaders for COBOL, Word (.docx), Excel (.xlsx/.xlsm/.xlsb), HTML, and plain text; Excel loader performs two passes (computed values + formula expressions); XLSM loader adds VBA extraction via oletools
- `config.py`: loads `.env` and exposes all configuration constants
