# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — 2026-06-04 · `e457dcb`

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
