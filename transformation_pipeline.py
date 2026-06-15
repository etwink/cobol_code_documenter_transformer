"""Transformation pipeline: scan COBOL + docs → cluster → transform → document.

Entry point:

    pipeline = TransformationPipeline(context_block="...")
    output   = pipeline.run(["path/to/cobol/folder"])

The output contains:
  - transformations  : one PythonTransformationResult per COBOL file
  - documentation    : one TransformationDocument (the full tech doc)
  - all_etl_operations : aggregated list of every ETL operation found
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from folder_scanner import FolderScanner, COBOL_EXTENSIONS
from cobol_dependency_analyzer import parse_file, build_dependency_graph
from cluster_builder import ClusterBuilder, DocumentCluster
from hierarchical_summarizer import HierarchicalSummarizer, ClusterSummary
from cobol_transformer import CobolToPythonTransformer, PythonTransformationResult
from etl_detector import ETLOperation
from llm_integration import AzureLLMClient, PromptBuilder


# ---------------------------------------------------------------------------
# Output data structures
# ---------------------------------------------------------------------------

@dataclass
class TransformationDocument:
    """Every section of the generated technical documentation."""
    overview: str             = ""
    assumptions: str          = ""
    python_db_architecture: str   = ""
    python_etl_architecture: str  = ""
    etl_interaction_steps: str    = ""
    database_operations: str      = ""
    business_logic: str           = ""
    data_flow: str                = ""
    decision_points: str          = ""
    systems_and_components: str   = ""
    appendix: str             = ""


@dataclass
class TransformationOutput:
    """Complete output of the transformation pipeline."""
    transformations: list[PythonTransformationResult] = field(default_factory=list)
    documentation: TransformationDocument | None = None
    all_etl_operations: list[ETLOperation] = field(default_factory=list)
    cluster_summaries: list[ClusterSummary] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class TransformationPipeline:
    """Orchestrate the full COBOL → Python transformation and documentation run."""

    def __init__(self, context_block: str = ""):
        """
        context_block: optional plain-text description of the business process.
        Injected into every LLM prompt to improve relevance.
        """
        self.context_block = context_block
        self.llm = AzureLLMClient()
        self.transformer = CobolToPythonTransformer(context_block=context_block)

    def run(
        self,
        input_path: str | Path,
        recursive: bool = True,
        progress_callback=None,
        output_dir: Path | str | None = None,
    ) -> TransformationOutput:
        """
        Run the full pipeline against a single input directory.

        input_path:       the directory containing COBOL source files and docs.
        output_dir:       where to write results; when provided the pipeline
                          automatically excludes it from the scan so re-runs
                          do not pick up previously generated Python files.
        progress_callback(stage, current, total) is called at key milestones.
        """
        def _progress(stage: str, current: int, total: int) -> None:
            if progress_callback:
                progress_callback(stage, current, total)

        # ── 1. Scan input files ──────────────────────────────────────────────
        _progress("Scanning files", 0, 1)
        scanner = FolderScanner()
        resolved = [Path(input_path).resolve()]
        # Exclude the output directory so re-runs don't pick up generated files
        exclude: set[Path] = set()
        if output_dir:
            exclude.add(Path(output_dir).resolve())
        scanned = scanner.scan(resolved, recursive=recursive, exclude_paths=exclude)
        _progress(f"Scan complete — {scanned.summary()}", 1, 1)

        if not scanned.cobol:
            searched = ", ".join(str(p) for p in resolved)
            extensions = ", ".join(sorted(COBOL_EXTENSIONS))
            raise ValueError(
                f"No COBOL files found.\n"
                f"  Paths searched : {searched}\n"
                f"  Extensions     : {extensions}\n"
                f"  Other files    : {scanned.summary()}\n\n"
                f"Check that DOCUMENTS_PATH in .env points to the folder containing "
                f"your COBOL source files and that the files use one of the supported "
                f"extensions listed above."
            )

        # ── 2. Build clusters ────────────────────────────────────────────────
        _progress("Building clusters", 0, 1)
        cluster_builder = ClusterBuilder()
        clusters = cluster_builder.build_clusters(
            cobol_files=scanned.cobol,
            word_files=scanned.word + scanned.other,  # .txt treated as documentation
            excel_files=scanned.excel,
            code_files=scanned.code,
            context_block=self.context_block,
        )

        # ── 3. Summarize clusters ────────────────────────────────────────────
        summarizer = HierarchicalSummarizer()
        cluster_summaries = summarizer.summarize_all(
            clusters,
            progress_callback=lambda name, i, t: _progress(f"Summarizing: {name}", i, t),
            context_block=self.context_block,
        )

        # ── 4. Build dependency context and system map ───────────────────────
        analyses    = [parse_file(p) for p in scanned.cobol]
        graph       = build_dependency_graph(analyses)
        dep_context = _dep_context_from_graph(graph)
        system_map  = _system_map_from_graph(scanned.cobol, graph)

        # ── 5. Aggregate documentation context from cluster summaries ────────
        doc_context = "\n\n".join(cs.summary for cs in cluster_summaries)

        # ── 6. Transform each COBOL file (topological order: callees before callers) ──
        transformations: list[PythonTransformationResult] = []
        sorted_cobol = _topological_sort(scanned.cobol, graph)
        known_interfaces: dict[str, str] = {}  # stem.upper() -> extracted Python interface text

        total_cobol = len(sorted_cobol)
        for idx, cobol_path in enumerate(sorted_cobol):
            _progress(f"Transforming {cobol_path.name}", idx + 1, total_cobol)
            dep_interfaces = _get_dep_interfaces(cobol_path.stem.upper(), graph, known_interfaces)
            try:
                result = self.transformer.transform(
                    cobol_path,
                    dependency_context=dep_context,
                    documentation_context=doc_context[:4_000],
                    system_context=system_map,
                    known_interfaces=dep_interfaces,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Transformation failed for {cobol_path.name} "
                    f"({idx + 1}/{total_cobol}): {exc}"
                ) from exc
            known_interfaces[cobol_path.stem.upper()] = _extract_python_interface(result.python_db_code)
            transformations.append(result)

        # ── 7. Aggregate ETL operations ──────────────────────────────────────
        all_etl_ops = [op for t in transformations for op in t.etl_operations]

        # ── 8. Generate documentation ────────────────────────────────────────
        _progress("Generating documentation", 0, 1)
        doc = _DocumentationBuilder(self.llm, self.context_block).build(
            cluster_summaries=cluster_summaries,
            transformations=transformations,
            all_etl_ops=all_etl_ops,
            progress_callback=progress_callback,
        )

        return TransformationOutput(
            transformations=transformations,
            documentation=doc,
            all_etl_operations=all_etl_ops,
            cluster_summaries=cluster_summaries,
        )


# ---------------------------------------------------------------------------
# Documentation builder (internal)
# ---------------------------------------------------------------------------

class _DocumentationBuilder:

    _TOKENS_PER_SECTION = 12_000

    # Injected into every section prompt to enforce dual-audience structure.
    _DUAL_AUDIENCE = (
        "\nAUDIENCE — This documentation is read by two groups:\n"
        "  • PRODUCT OWNERS: business stakeholders who need to understand WHAT the system\n"
        "    does, WHY it exists, what business rules it enforces, and what outcomes it\n"
        "    produces. They do not need code-level details.\n"
        "  • DEVELOPERS: engineers who need to understand HOW the system works technically,\n"
        "    what libraries and patterns are used, and how to maintain or extend it.\n\n"
        "Structure every major topic using this two-part format:\n"
        "  > Business need: [plain English — purpose, business rule, or outcome; no code terms]\n"
        "  > Technical detail: [implementation — libraries, functions, file names, patterns]\n\n"
        "If a point has no meaningful business dimension, label it [Developers only].\n"
        "If a point is pure business context with no code implication, label it [Product Owners only].\n"
    )

    def __init__(self, llm: AzureLLMClient, context_block: str):
        self.llm = llm
        self.ctx = context_block

    def build(
        self,
        cluster_summaries: list[ClusterSummary],
        transformations: list[PythonTransformationResult],
        all_etl_ops: list[ETLOperation],
        progress_callback=None,
    ) -> TransformationDocument:
        cluster_texts = [cs.summary for cs in cluster_summaries]
        all_assumptions = [a for t in transformations for a in t.assumptions]
        write_ops = [op for op in all_etl_ops if not op.is_read]

        sections = [
            ("Assumptions",            lambda: self._assumptions(all_assumptions)),
            ("Overview",               lambda: self._overview(cluster_texts, transformations)),
            ("ETL Interaction Steps",  lambda: self._etl_steps(write_ops, cluster_texts)),
            ("Python DB Architecture", lambda: self._python_db_arch(cluster_texts, transformations)),
            ("Python ETL Architecture",lambda: self._python_etl_arch(cluster_texts, transformations, write_ops)),
            ("Database Operations",    lambda: self._db_operations(all_etl_ops, cluster_texts)),
            ("Business Logic",         lambda: self._business_logic(cluster_texts)),
            ("Data Flow",              lambda: self._data_flow(cluster_texts, all_etl_ops)),
            ("Decision Points",        lambda: self._decision_points(cluster_texts)),
            ("Systems and Components", lambda: self._systems(cluster_texts)),
            ("Appendix",               lambda: self._appendix(cluster_texts, all_etl_ops)),
        ]

        section_map: dict[str, str] = {}
        total = len(sections)
        for i, (label, fn) in enumerate(sections):
            if progress_callback:
                progress_callback(f"Documentation: {label}", i + 1, total)
            section_map[label] = fn()

        return TransformationDocument(
            assumptions=section_map["Assumptions"],
            overview=section_map["Overview"],
            etl_interaction_steps=section_map["ETL Interaction Steps"],
            python_db_architecture=section_map["Python DB Architecture"],
            python_etl_architecture=section_map["Python ETL Architecture"],
            database_operations=section_map["Database Operations"],
            business_logic=section_map["Business Logic"],
            data_flow=section_map["Data Flow"],
            decision_points=section_map["Decision Points"],
            systems_and_components=section_map["Systems and Components"],
            appendix=section_map["Appendix"],
        )

    # ── Section writers ──────────────────────────────────────────────────────

    def _assumptions(self, raw_assumptions: list[str]) -> str:
        if not raw_assumptions:
            return (
                "No explicit assumptions were recorded during transformation. "
                "The source code provided sufficient context to perform the conversion "
                "without requiring significant inference about missing process boundaries."
            )
        bullet_block = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(raw_assumptions))
        prompt = (
            f"You are writing the ASSUMPTIONS section of a technical migration document "
            f"for a COBOL-to-Python conversion project.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"The following assumptions were recorded during the transformation because "
            f"the COBOL source did not provide complete visibility into the beginning or "
            f"end of the business process:\n\n"
            f"{bullet_block}\n\n"
            f"Write a well-organized ASSUMPTIONS section (400–700 words) that:\n"
            f"1. Opens with a brief explanation of WHY assumptions were necessary — "
            f"frame it in terms both a product owner and a developer will understand\n"
            f"2. Groups assumptions into clear categories:\n"
            f"   a. Process Triggers and Scheduling\n"
            f"   b. Pre-conditions and Input Requirements\n"
            f"   c. Outputs and Downstream Consumers\n"
            f"   d. Business Process Context\n"
            f"   e. Scope and Boundary Assumptions\n"
            f"3. For each assumption: the business impact of getting it wrong, the technical "
            f"evidence from the code, and a confidence level (HIGH / MEDIUM / LOW)\n"
            f"4. Closes with what each audience needs to do to validate these assumptions: "
            f"what a product owner should confirm with business stakeholders, and what a "
            f"developer should verify in the codebase or environment\n\n"
            f"Write in plain prose with sub-headings. No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _overview(
        self,
        cluster_texts: list[str],
        transformations: list[PythonTransformationResult],
    ) -> str:
        notes = "\n\n".join(t.transformation_notes for t in transformations[:8])
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the OVERVIEW section of a technical document for a COBOL-to-Python migration.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries (what the original COBOL system does):\n{combined}\n\n"
            f"Transformation summary per file:\n{notes}\n\n"
            f"Write a comprehensive OVERVIEW (500–800 words) structured so that both a product owner "
            f"and a developer can orient themselves. Cover:\n"
            f"1. Business need: why this system exists, what business problem it solves, who uses it, "
            f"and what outcome it produces — in plain language for product owners\n"
            f"2. Technical scope: which programs were transformed, what types of operations are involved — "
            f"for developers\n"
            f"3. The two Python versions produced:\n"
            f"   - DB Version (business view): identical behavior to the original COBOL system\n"
            f"   - DB Version (technical view): real SQLAlchemy database calls, # DB_OPERATION: markers\n"
            f"   - ETL/File Version (business view): write operations are staged for an ETL pipeline\n"
            f"   - ETL/File Version (technical view): CSV staging files, # ETL_STEP: markers, ETL contract block\n"
            f"4. Key decisions made during the conversion and their business/technical rationale\n"
            f"5. A navigation guide: which sections a product owner should read vs. a developer\n"
            f"6. Known limitations or caveats relevant to each audience\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _etl_steps(self, write_ops: list[ETLOperation], cluster_texts: list[str]) -> str:
        if not write_ops:
            return (
                "No database write, insert, update, or delete operations were detected "
                "in this codebase by static analysis. The system appears to be read-only "
                "or write operations use patterns not captured by the static detector.\n\n"
                "Review the source code manually if this is unexpected."
            )
        ops_block = "\n".join(
            f"  {i+1:>3}. Line {op.line_number:>5} | [{op.operation_type.value:<16}] "
            f"{op.description:<50} → etl_out_{op.table_or_file.lower()}.csv"
            for i, op in enumerate(write_ops)
        )
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the ETL INTERACTION STEPS section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Context (what the system does):\n{combined[:3000]}\n\n"
            f"The following COBOL write/modify operations were detected by static analysis "
            f"and converted to ETL staging file writes in the ETL version of the Python code:\n\n"
            f"{ops_block}\n\n"
            f"Write the ETL INTERACTION STEPS section (600–900 words) covering both perspectives:\n\n"
            f"Business need (product owner view):\n"
            f"  - Why these data movements exist: what business event triggers them, what records "
            f"    are affected, and what the business outcome is for each step\n"
            f"  - What happens to the business if a step is skipped or fails\n"
            f"  - Who owns each data domain (which team or system is the authoritative source)\n\n"
            f"Technical detail (developer view):\n"
            f"  - A table of EVERY ETL step: Step # | Operation Type | Source Table/Dataset | "
            f"Staging File Name | What the ETL Job Must Do\n"
            f"  - The expected sequence in which staging files are produced\n"
            f"  - Dependencies between steps (e.g., inserts before updates on the same table)\n"
            f"  - The # ETL_STEP: comment marker convention and how to trace steps in the code\n"
            f"  - Read-only DB queries that are NOT ETL steps (kept in both versions)\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _python_db_arch(
        self,
        cluster_texts: list[str],
        transformations: list[PythonTransformationResult],
    ) -> str:
        combined = _clusters_block(cluster_texts)
        modules = "\n".join(
            f"  - {Path(t.source_file).stem}.py  (from COBOL: {Path(t.source_file).name})"
            for t in transformations
        )
        prompt = (
            f"You are writing the PYTHON CODE — DATABASE VERSION section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Context (what the COBOL system does):\n{combined[:3000]}\n\n"
            f"Python modules produced:\n{modules}\n\n"
            f"Write this section (600–900 words) covering both perspectives:\n\n"
            f"Business need (product owner view):\n"
            f"  - What this version of the code does from a business perspective — it is a "
            f"    functionally equivalent replacement for the original COBOL program\n"
            f"  - What business capabilities it preserves: the same rules, the same data, "
            f"    the same outcomes as the original system\n"
            f"  - Any business behavior changes introduced by the conversion (there should be none, "
            f"    but flag any if present)\n\n"
            f"Technical detail (developer view):\n"
            f"  - Module structure: one sub-section per Python module — purpose, key classes/functions, "
            f"    mapping to the original COBOL\n"
            f"  - Database connectivity: SQLAlchemy engine/session setup, connection string config\n"
            f"  - How database operations are structured: parameterized queries, ORM vs Core, "
            f"    transaction boundaries, rollback conditions\n"
            f"  - The # DB_OPERATION: comment convention for navigating the code\n"
            f"  - Error handling for database failures\n"
            f"  - Runtime dependencies and how to run this version\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _python_etl_arch(
        self,
        cluster_texts: list[str],
        transformations: list[PythonTransformationResult],
        write_ops: list[ETLOperation],
    ) -> str:
        combined = _clusters_block(cluster_texts)
        staging_files = sorted({
            f"etl_out_{op.table_or_file.lower()}.csv" for op in write_ops
        })
        files_block = "\n".join(f"  - {f}" for f in staging_files) or "  (none detected)"
        prompt = (
            f"You are writing the PYTHON CODE — ETL/FILE VERSION section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Context (what the COBOL system does):\n{combined[:3000]}\n\n"
            f"Staging files this version produces:\n{files_block}\n\n"
            f"Write this section (600–900 words) covering both perspectives:\n\n"
            f"Business need (product owner view):\n"
            f"  - Why this version exists: the business reason for routing writes through an ETL "
            f"    pipeline instead of directly to the database (auditability, data governance, "
            f"    decoupling, environment constraints, or other)\n"
            f"  - What business data is staged and what the downstream ETL job does with it\n"
            f"  - The business impact if the ETL job does not run or runs out of sequence\n\n"
            f"Technical detail (developer view):\n"
            f"  - How this version differs from the DB version: only the I/O layer changes, "
            f"    business logic is identical\n"
            f"  - The ETL contract comment block at the top of each module and how to read it\n"
            f"  - Staging file naming convention, format (UTF-8 CSV with header row), schema\n"
            f"  - The # ETL_STEP: comment convention\n"
            f"  - Which database reads are kept in this version and why (read-only lookups)\n"
            f"  - How to run the ETL version, expected output, and how to verify correctness\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _db_operations(
        self, all_etl_ops: list[ETLOperation], cluster_texts: list[str]
    ) -> str:
        reads  = [op for op in all_etl_ops if op.is_read]
        writes = [op for op in all_etl_ops if not op.is_read]
        read_block  = "\n".join(f"  - Line {op.line_number}: {op.description}" for op in reads)  or "  (none)"
        write_block = "\n".join(f"  - Line {op.line_number}: {op.description}" for op in writes) or "  (none)"
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the DATABASE OPERATIONS section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Context:\n{combined[:2000]}\n\n"
            f"Read operations:\n{read_block}\n\n"
            f"Write / modify operations:\n{write_block}\n\n"
            f"Write this section (500–700 words) covering both perspectives:\n\n"
            f"Business need (product owner view):\n"
            f"  - What business information is read: what the data represents in business terms, "
            f"    who owns it, and why the system needs it\n"
            f"  - What business records are created or changed: what the write operations mean "
            f"    for the business (e.g., 'a new claim is recorded', 'a policy status is updated')\n"
            f"  - Business rules that govern when reads and writes happen\n\n"
            f"Technical detail (developer view):\n"
            f"  - Named inventory of every table and dataset accessed\n"
            f"  - Read operations: query structure, key filter conditions, expected result sets\n"
            f"  - Write operations: data inserted/updated/deleted and under what conditions\n"
            f"  - Data transformations applied to query results\n"
            f"  - Transaction boundaries and rollback conditions\n"
            f"  - In the ETL version: which become staging files vs. which remain live DB calls\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _business_logic(self, cluster_texts: list[str]) -> str:
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the BUSINESS LOGIC section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined}\n\n"
            f"Write this section (500–700 words) covering the non-ETL processing logic "
            f"(identical in both Python versions) from both perspectives:\n\n"
            f"Business need (product owner view) — for each major logic area:\n"
            f"  - What business rule or requirement this logic fulfills\n"
            f"  - What the business outcome is when the rule passes vs. fails\n"
            f"  - Any compliance, regulatory, or policy context behind the rule\n\n"
            f"Technical detail (developer view) — for each logic area:\n"
            f"  - Calculations: formulas, rates, totals, rounding rules\n"
            f"  - Validation rules: what is checked, what action is taken on failure\n"
            f"  - Branching and conditional logic: key IF/EVALUATE decisions\n"
            f"  - Loops and iteration: what they process, exit conditions\n"
            f"  - Status codes, flags, and return codes\n"
            f"  - String and data formatting operations\n\n"
            f"Do NOT include database I/O — that belongs in Database Operations. "
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _data_flow(
        self, cluster_texts: list[str], all_etl_ops: list[ETLOperation]
    ) -> str:
        combined = _clusters_block(cluster_texts)
        ops_seq = "\n".join(
            f"  {i+1}. [{'READ' if op.is_read else 'WRITE'}] {op.description}"
            for i, op in enumerate(all_etl_ops)
        ) or "  (no data operations detected)"
        prompt = (
            f"You are writing the DATA FLOW section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined[:2500]}\n\n"
            f"Data operations in detected sequence:\n{ops_seq}\n\n"
            f"Write this section (500–700 words) tracing the complete data lifecycle "
            f"from both perspectives:\n\n"
            f"Business need (product owner view):\n"
            f"  - What business information enters the system, where it comes from, "
            f"    and what it represents in business terms\n"
            f"  - What happens to the data as it moves through the process — the business "
            f"    transformations and decisions applied\n"
            f"  - What business information leaves the system and who or what uses it\n\n"
            f"Technical detail (developer view):\n"
            f"  - Inputs: sources (files, DB tables, parameters), formats, data types\n"
            f"  - Processing stages: how data moves and is transformed step-by-step\n"
            f"  - Intermediate state: working storage, temporary tables, in-memory structures\n"
            f"  - Outputs: destination, format, encoding\n"
            f"  - ETL version: which staging files are produced and at which stage\n\n"
            f"Follow a logical sequential flow from input to output. No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _decision_points(self, cluster_texts: list[str]) -> str:
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the DECISION POINTS section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined}\n\n"
            f"Write this section (500–700 words) covering every significant conditional logic "
            f"and business rule from both perspectives:\n\n"
            f"Business need (product owner view) — for each decision point:\n"
            f"  - The business rule or policy being enforced in plain English\n"
            f"  - The possible outcomes and what each means for the business\n"
            f"  - The business consequence of the rule being wrong or bypassed\n\n"
            f"Technical detail (developer view) — for each decision point:\n"
            f"  - The condition being evaluated (variable, flag, or data value)\n"
            f"  - The code path taken for each outcome (function called, branch taken)\n"
            f"  - Which program or module applies the decision\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _systems(self, cluster_texts: list[str]) -> str:
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the SYSTEMS AND COMPONENTS section of a technical migration document.\n"
            f"{self._DUAL_AUDIENCE}\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined}\n\n"
            f"Write this section (500–700 words) as a plain-language inventory of every system "
            f"and component involved, from both perspectives:\n\n"
            f"Business need (product owner view) — for each component:\n"
            f"  - What this component does in business terms\n"
            f"  - Which team or department owns it\n"
            f"  - What business capability would be lost if it were unavailable\n\n"
            f"Technical detail (developer view) — organize into categories:\n"
            f"  Core Processing Programs | Shared Modules & Copybooks | Input Data Sources |\n"
            f"  Output Files & Reports | Databases & Data Stores | External Interfaces |\n"
            f"  Batch / Scheduled Jobs\n"
            f"  For each: technical name, type, purpose, and how it connects to the rest\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _appendix(
        self, cluster_texts: list[str], all_etl_ops: list[ETLOperation]
    ) -> str:
        combined = _clusters_block(cluster_texts)
        staging_files = sorted({
            f"etl_out_{op.table_or_file.lower()}.csv"
            for op in all_etl_ops if not op.is_read
        })
        files_block = "\n".join(f"  - {f}" for f in staging_files) or "  (none)"
        prompt = (
            f"You are writing the APPENDIX of a technical migration document. "
            f"This is a reference section for both product owners and developers.\n\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined[:2000]}\n\n"
            f"Create the following sub-sections:\n\n"
            f"A. GLOSSARY — Define every technical term, acronym, system name, and domain-specific "
            f"phrase used in this document. List alphabetically. Each entry should be written so "
            f"that a product owner can understand it without technical background.\n\n"
            f"B. COMPONENT INDEX — A quick-reference list of every program, file, database, and "
            f"system mentioned. For each: name, type, one-sentence business purpose, and one-sentence "
            f"technical description.\n\n"
            f"C. ROLES AND RESPONSIBILITIES — Every role, team, or person type referenced. "
            f"For each: role name, which part of the process they own, and what they are responsible for. "
            f"Include both business roles (product owner, business analyst) and technical roles "
            f"(developer, ETL engineer, DBA).\n\n"
            f"D. ETL STAGING FILE REFERENCE — All ETL staging files produced by the ETL version:\n"
            f"{files_block}\n"
            f"For each file: name, target table/dataset, operation type (insert/update/delete), "
            f"and expected column schema.\n\n"
            f"E. KEY ASSUMPTIONS — List all assumptions made during transformation, noting what "
            f"a product owner should confirm with business stakeholders and what a developer "
            f"should verify in the codebase or environment.\n\n"
            f"No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dep_context_from_graph(graph: dict) -> str:
    # Deduplicate: same COBOL verb (CALL/COPY) can appear on many lines; show each
    # unique (source, dep_type, target) edge only once.
    seen: set[tuple] = set()
    lines = []
    for src, deps in sorted(graph.items()):
        for dep in deps:
            key = (src, dep["dep_type"], dep["target"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{src} --[{dep['dep_type']}]--> {dep['target']}")
    return "\n".join(lines)


def _system_map_from_graph(cobol_files: list[Path], graph: dict) -> str:
    """
    Build a human-readable map of the whole system for the LLM.

    Shows every COBOL program alongside its Python module name and
    which other modules it calls or is called by.  This is injected
    into every transformer prompt so the LLM can generate correct
    relative imports and understand the program hierarchy.
    """
    if not cobol_files:
        return ""

    # Build reverse map: who calls each program
    callers: dict[str, list[str]] = {}
    for src, deps in graph.items():
        for dep in deps:
            callers.setdefault(dep["target"], []).append(src)

    # All known stems (upper-case)
    all_stems = {p.stem.upper() for p in cobol_files}

    lines = [
        "All programs in this system (treat them as one connected Python package):",
        "",
    ]
    for p in sorted(cobol_files, key=lambda x: x.stem.upper()):
        stem = p.stem.upper()
        py_name = p.stem.lower() + ".py"
        calls = sorted({d["target"] for d in graph.get(stem, []) if d["target"] in all_stems})
        called_by = sorted(set(callers.get(stem, [])) & all_stems)

        role = "entry point" if stem not in callers else "subprogram/utility"
        lines.append(f"  {stem:<20} → {py_name:<30} [{role}]")
        if calls:
            lines.append(f"    calls    : {', '.join(calls)}")
        if called_by:
            lines.append(f"    called by: {', '.join(called_by)}")

    lines += [
        "",
        "Python import convention for this package:",
        "  from . import <module_stem>   (for CALL dependencies)",
        "  from . import <copybook_stem>  (for COPY dependencies)",
    ]
    return "\n".join(lines)


def _clusters_block(cluster_texts: list[str]) -> str:
    return "\n\n".join(
        f"--- Cluster {i+1} ---\n{t}" for i, t in enumerate(cluster_texts)
    )


def _topological_sort(cobol_files: list[Path], graph: dict) -> list[Path]:
    """Return files in leaf-first (callees before callers) order using Kahn's algorithm.

    Processing callees first means each module's public interface is extracted before
    callers are transformed, so callers receive exact function signatures.
    Isolated files (no dependencies in either direction) go first.
    Cycles are broken by appending remaining nodes in original order.
    """
    stem_to_path = {p.stem.upper(): p for p in cobol_files}
    all_stems = set(stem_to_path)

    # Build reversed graph: callee -> [callers] and in-degree in the reversed graph.
    # In the reversed graph, in-degree[A] = number of things A calls in the original graph.
    # Nodes with in-degree 0 in the reversed graph = pure leaves (call nobody) → process first.
    in_degree: dict[str, int] = {s: 0 for s in all_stems}
    reversed_adj: dict[str, list[str]] = {s: [] for s in all_stems}

    for src, deps in graph.items():
        if src not in all_stems:
            continue
        for dep in deps:
            tgt = dep["target"]
            if tgt not in all_stems:
                continue
            reversed_adj[tgt].append(src)
            in_degree[src] += 1

    queue = deque(s for s, deg in in_degree.items() if deg == 0)
    sorted_stems: list[str] = []

    while queue:
        node = queue.popleft()
        sorted_stems.append(node)
        for neighbor in reversed_adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Append any remaining nodes (cycles) in original file order
    covered = set(sorted_stems)
    for p in cobol_files:
        if p.stem.upper() not in covered:
            sorted_stems.append(p.stem.upper())

    return [stem_to_path[s] for s in sorted_stems if s in stem_to_path]


def _extract_python_interface(python_code: str) -> str:
    """Parse generated Python with ast and return the public interface.

    Extracts, in order:
      1. Module-level ALL_CAPS constants (e.g. FIELD_NAMES, _FIELD_MAX_LENGTHS) —
         includes private-prefixed names because copybook modules use them for
         field metadata that callers need for validation and ETL file schema.
      2. Top-level public class definitions with annotated fields (TypedDicts,
         dataclasses, NamedTuples) so callers know every type in function signatures.
      3. Top-level public function signatures.

    Only top-level nodes are included — class methods and nested functions are
    implementation details, not part of the callable interface.

    Note: inline comments (e.g. field data-type hints written as # comments rather
    than type annotations) are discarded by ast.parse and cannot be recovered here.

    Returns an empty string if the code cannot be parsed (e.g. LLM error output).
    """
    try:
        tree = ast.parse(python_code)
    except SyntaxError:
        return ""

    def _is_constant_name(name: str) -> bool:
        """True when name follows the ALL_CAPS constant convention.

        Matches FIELD_NAMES, _FIELD_MAX_LENGTHS, MAX_SIZE, etc.
        Rejects field_names, FieldNames, _logger, _session, etc.
        """
        base = name.lstrip("_")
        return bool(base) and base == base.upper() and base.replace("_", "").isalnum()

    lines: list[str] = []
    top_level = list(ast.iter_child_nodes(tree))

    # ── 1. Module-level constants (ALL_CAPS, including _PRIVATE_CAPS) ────────
    const_lines: list[str] = []
    for node in top_level:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if _is_constant_name(name) and node.value is not None:
                ann = f": {ast.unparse(node.annotation)}"
                const_lines.append(f"  {name}{ann} = {ast.unparse(node.value)}")
        elif isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if _is_constant_name(name):
                    const_lines.append(f"  {name} = {ast.unparse(node.value)}")
    if const_lines:
        lines.extend(const_lines)
        lines.append("")

    # ── 2. Public class definitions ──────────────────────────────────────────
    for node in top_level:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name.startswith("_"):
            continue

        for dec in node.decorator_list:
            lines.append(f"  @{ast.unparse(dec)}")

        bases = [ast.unparse(b) for b in node.bases]
        base_str = f"({', '.join(bases)})" if bases else ""
        lines.append(f"  class {node.name}{base_str}:")

        fields = [
            f"    {item.target.id}: {ast.unparse(item.annotation)}"
            + (f" = {ast.unparse(item.value)}" if item.value else "")
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        ]
        lines.extend(fields if fields else ["    ..."])
        lines.append("")

    # ── 3. Public top-level functions ─────────────────────────────────────────
    for node in top_level:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_"):
            continue

        func_args = node.args
        args: list[str] = []

        n_defaults = len(func_args.defaults)
        n_pos = len(func_args.args) - n_defaults
        for i, arg in enumerate(func_args.args):
            ann = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
            if i >= n_pos:
                default = f" = {ast.unparse(func_args.defaults[i - n_pos])}"
                args.append(f"{arg.arg}{ann}{default}")
            else:
                args.append(f"{arg.arg}{ann}")

        if func_args.vararg:
            ann = f": {ast.unparse(func_args.vararg.annotation)}" if func_args.vararg.annotation else ""
            args.append(f"*{func_args.vararg.arg}{ann}")
        if func_args.kwarg:
            ann = f": {ast.unparse(func_args.kwarg.annotation)}" if func_args.kwarg.annotation else ""
            args.append(f"**{func_args.kwarg.arg}{ann}")

        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        sig = f"  def {node.name}({', '.join(args)}){ret}: ..."

        docstring = ast.get_docstring(node)
        if docstring:
            first_line = docstring.split("\n")[0][:100]
            lines.append(f"{sig}  # {first_line}")
        else:
            lines.append(sig)

    return "\n".join(lines)


def _get_dep_interfaces(
    stem: str,
    graph: dict,
    known_interfaces: dict[str, str],
) -> str:
    """Return a formatted context block of known interfaces for a file's dependencies.

    Covers both CALL dependencies (callable subprograms) and COPY dependencies
    (copybooks — may define data structures, constants, or paragraphs).
    Each unique target is listed exactly once regardless of how many times the
    same CALL/COPY appears in the COBOL source.

    The known_interfaces value is:
      None   — dependency not yet transformed (processed later in topological order)
      ""     — transformed but no public functions found (data-only copybook)
      <text> — transformed and has callable public functions
    """
    # Deduplicate by target name; preserve first-occurrence order
    seen_targets: set[str] = set()
    deps: list[dict] = []
    for d in graph.get(stem, []):
        if d.get("dep_type") in ("CALL", "COPY", "SQL INCLUDE") and d["target"] not in seen_targets:
            seen_targets.add(d["target"])
            deps.append(d)

    if not deps:
        return ""

    lines = [
        "KNOWN DEPENDENCY INTERFACES — use EXACTLY these signatures when calling other modules.",
        "Do not invent different parameter names, counts, or types.",
    ]
    for dep in deps:
        tgt = dep["target"]
        dep_type = dep.get("dep_type", "CALL")
        if dep_type == "COPY":
            label = "COPY — shared data structures / constants"
        elif dep_type == "SQL INCLUDE":
            label = "SQL INCLUDE — DB2 DCLGEN table declarations"
        else:
            label = "CALL"
        # Use .get with sentinel to distinguish "not in dict yet" from "empty string"
        iface = known_interfaces.get(tgt, None)

        lines.append(f"\n  {tgt.lower()}.py:  [{label}]")
        if iface is None:
            lines.append(
                "    # (not yet transformed — add '# TODO: verify signature' on every call)"
            )
        elif iface:
            lines.append(iface)
        else:
            lines.append(
                "    # (no public functions — MUST still import this module for its data structures,"
                " record layouts, or constants. Do not redefine them locally."
                " In the ETL version, use its field names as pipe-delimited column headers.)"
            )

    return "\n".join(lines)
