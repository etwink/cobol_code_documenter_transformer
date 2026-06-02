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

from dataclasses import dataclass, field
from pathlib import Path

from folder_scanner import FolderScanner
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
        self.transformer = CobolToPythonTransformer()

    def run(
        self,
        input_paths: list[str | Path],
        recursive: bool = True,
        progress_callback=None,
    ) -> TransformationOutput:
        """
        Run the full pipeline.

        progress_callback(stage: str, current: int, total: int) is called
        at key milestones if provided.
        """
        def _progress(stage: str, current: int, total: int) -> None:
            if progress_callback:
                progress_callback(stage, current, total)

        # ── 1. Scan input files ──────────────────────────────────────────────
        _progress("Scanning files", 0, 1)
        scanner = FolderScanner()
        scanned = scanner.scan([Path(p) for p in input_paths], recursive=recursive)

        # ── 2. Build clusters ────────────────────────────────────────────────
        _progress("Building clusters", 0, 1)
        cluster_builder = ClusterBuilder()
        clusters = cluster_builder.build_clusters(
            cobol_files=scanned.cobol,
            word_files=scanned.word,
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

        # ── 4. Build dependency context string ───────────────────────────────
        dep_context = _build_dep_context(scanned.cobol)

        # ── 5. Aggregate documentation context from cluster summaries ────────
        doc_context = "\n\n".join(cs.summary for cs in cluster_summaries)

        # ── 6. Transform each COBOL file ─────────────────────────────────────
        transformations: list[PythonTransformationResult] = []
        total_cobol = len(scanned.cobol)
        for idx, cobol_path in enumerate(scanned.cobol):
            _progress(f"Transforming {cobol_path.name}", idx + 1, total_cobol)
            result = self.transformer.transform(
                cobol_path,
                dependency_context=dep_context,
                documentation_context=doc_context[:4_000],
            )
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

    _TOKENS_PER_SECTION = 6_000

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
            f"for a COBOL-to-Python conversion project.\n\n"
            f"{self.ctx}\n\n"
            f"The following assumptions were recorded during the transformation because "
            f"the COBOL source did not provide complete visibility into the beginning or "
            f"end of the business process:\n\n"
            f"{bullet_block}\n\n"
            f"Write a well-organized ASSUMPTIONS section (400–700 words) that:\n"
            f"1. Opens with a brief explanation of WHY assumptions were necessary "
            f"(incomplete process boundaries, partial source visibility, missing calling context)\n"
            f"2. Groups assumptions into clear categories:\n"
            f"   a. Process Triggers and Scheduling\n"
            f"   b. Pre-conditions and Input Requirements\n"
            f"   c. Outputs and Downstream Consumers\n"
            f"   d. Business Process Context\n"
            f"   e. Scope and Boundary Assumptions\n"
            f"3. For each assumption: states it clearly, notes the evidence from the code, "
            f"and gives a confidence level (HIGH / MEDIUM / LOW)\n"
            f"4. Closes with a paragraph on what additional information would be needed "
            f"to validate or refine these assumptions\n\n"
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
            f"You are writing the OVERVIEW section of a technical document for a COBOL-to-Python migration.\n\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries (what the original COBOL system does):\n{combined}\n\n"
            f"Transformation summary per file:\n{notes}\n\n"
            f"Write a comprehensive OVERVIEW (500–800 words) covering:\n"
            f"1. What the original COBOL system does and why it exists (business purpose)\n"
            f"2. What was transformed: which programs, what types of operations are involved\n"
            f"3. The two Python versions produced and how they differ:\n"
            f"   - DB Version: real database calls, identical to original COBOL behavior\n"
            f"   - ETL/File Version: write operations replaced with staging files for a downstream ETL job\n"
            f"4. Key architectural decisions made during the conversion\n"
            f"5. How to read this documentation (which sections to consult for what)\n"
            f"6. Any significant limitations or caveats about the conversion\n\n"
            f"Write for a technical reader who is new to this system. No markdown fences."
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
            f"{op.description:<50} → etl_stage_{op.table_or_file.lower()}.csv"
            for i, op in enumerate(write_ops)
        )
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the ETL INTERACTION STEPS section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Context (what the system does):\n{combined[:3000]}\n\n"
            f"The following COBOL write/modify operations were detected by static analysis "
            f"and converted to ETL staging file writes in the ETL version of the Python code:\n\n"
            f"{ops_block}\n\n"
            f"Write the ETL INTERACTION STEPS section (600–900 words) covering:\n"
            f"1. An explanation of the ETL pattern: how the file-based Python version works, "
            f"what staging files it produces, and what a downstream ETL job must do with them\n"
            f"2. A clear table or structured list of EVERY ETL step with columns:\n"
            f"   Step # | Operation Type | Source Table/Dataset | Staging File Name | "
            f"What the ETL Job Must Do (insert/update/delete target)\n"
            f"3. The expected sequence in which staging files are produced (critical for the ETL job)\n"
            f"4. Dependencies between ETL steps (e.g., inserts before updates on the same table)\n"
            f"5. The # ETL_STEP: comment marker convention used in the Python code — "
            f"how to locate and trace each step\n"
            f"6. Steps that do NOT require ETL interaction (read-only DB queries kept in both versions)\n\n"
            f"This section will be read by the ETL engineering team. Be precise and actionable. "
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
            f"You are writing the PYTHON CODE — DATABASE VERSION section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Context (what the COBOL system does):\n{combined[:3000]}\n\n"
            f"Python modules produced:\n{modules}\n\n"
            f"Write this section (600–900 words) covering:\n"
            f"1. The module structure of the DB version: one sub-section per Python module "
            f"covering its purpose, key classes/functions, and how it maps to the COBOL original\n"
            f"2. Database connectivity: how the SQLAlchemy engine and session are set up, "
            f"connection string configuration, and connection pooling\n"
            f"3. How database operations are structured: parameterized queries vs ORM, "
            f"transaction boundaries, and rollback conditions\n"
            f"4. The # DB_OPERATION: comment convention — how to navigate the code using it\n"
            f"5. Error handling for database failures\n"
            f"6. How to run this version and what runtime dependencies are required\n\n"
            f"Write for a developer who will deploy and maintain this code. No markdown fences."
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
            f"etl_stage_{op.table_or_file.lower()}.csv" for op in write_ops
        })
        files_block = "\n".join(f"  - {f}" for f in staging_files) or "  (none detected)"
        prompt = (
            f"You are writing the PYTHON CODE — ETL/FILE VERSION section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Context (what the COBOL system does):\n{combined[:3000]}\n\n"
            f"Staging files this version produces:\n{files_block}\n\n"
            f"Write this section (600–900 words) covering:\n"
            f"1. How the ETL version differs from the DB version (only the I/O layer changes)\n"
            f"2. The ETL contract comment block at the top of each module — what it contains "
            f"and how an ETL engineer should read it\n"
            f"3. Staging file naming convention, format (CSV with header row), and encoding (UTF-8)\n"
            f"4. The # ETL_STEP: comment convention\n"
            f"5. Which database reads are KEPT in this version and why (read-only lookups)\n"
            f"6. How to run the ETL version, what output to expect, and how to verify it\n"
            f"7. What the downstream ETL job needs to do with each staging file\n\n"
            f"Write for both the Python developer and the ETL integration team. No markdown fences."
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
            f"You are writing the DATABASE OPERATIONS section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Context:\n{combined[:2000]}\n\n"
            f"Read operations:\n{read_block}\n\n"
            f"Write / modify operations:\n{write_block}\n\n"
            f"Write this section (500–700 words) covering:\n"
            f"1. Every table and dataset accessed by this system (a named inventory)\n"
            f"2. Read operations: what data is queried, key filter conditions, expected result sets\n"
            f"3. Write operations: what data is inserted, updated, or deleted, and under what conditions\n"
            f"4. Any data transformation applied to query results before use\n"
            f"5. Transaction boundaries: what is committed together, rollback conditions\n"
            f"6. In the ETL version: which of these become staging files vs. which remain live DB calls\n\n"
            f"Distinguish reads from writes clearly. No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _business_logic(self, cluster_texts: list[str]) -> str:
        combined = _clusters_block(cluster_texts)
        prompt = (
            f"You are writing the BUSINESS LOGIC section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined}\n\n"
            f"Write this section (500–700 words) covering ONLY the non-ETL processing logic "
            f"(this logic is IDENTICAL in both Python versions):\n"
            f"1. Calculations and mathematical transformations (formulas, rates, totals, rounding)\n"
            f"2. Validation rules: what is checked, what action is taken on failure\n"
            f"3. Branching and conditional logic: key IF/EVALUATE decisions and their outcomes\n"
            f"4. Loops and iteration patterns: what they process and their exit conditions\n"
            f"5. Status codes, flags, and return codes managed by the program\n"
            f"6. String and data formatting operations\n\n"
            f"Do NOT include database I/O here — that belongs in Database Operations. "
            f"Focus on logic that would exist regardless of whether the system uses a DB or files. "
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
            f"You are writing the DATA FLOW section of a technical migration document.\n\n"
            f"{self.ctx}\n\n"
            f"Cluster summaries:\n{combined[:2500]}\n\n"
            f"Data operations in detected sequence:\n{ops_seq}\n\n"
            f"Write this section (500–700 words) tracing the complete data lifecycle:\n"
            f"1. Inputs: what enters the system, from where (files, DB tables, parameters), "
            f"in what format, and what it represents\n"
            f"2. Processing stages: step-by-step how data moves and is transformed\n"
            f"3. Intermediate state: working storage, temporary tables, in-memory structures\n"
            f"4. Outputs: what leaves the system, to which destination, in which format\n"
            f"5. ETL version differences: which staging files are produced and at which stage\n\n"
            f"Follow a logical sequential flow from input to output. No markdown fences."
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _decision_points(self, cluster_texts: list[str]) -> str:
        prompt = PromptBuilder.build_section_prompt_from_clusters(
            "decision_points", cluster_texts, self.ctx
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _systems(self, cluster_texts: list[str]) -> str:
        prompt = PromptBuilder.build_section_prompt_from_clusters(
            "systems_and_components", cluster_texts, self.ctx
        )
        return self.llm.query(prompt, max_tokens=self._TOKENS_PER_SECTION)

    def _appendix(
        self, cluster_texts: list[str], all_etl_ops: list[ETLOperation]
    ) -> str:
        staging_files = sorted({
            f"etl_stage_{op.table_or_file.lower()}.csv"
            for op in all_etl_ops if not op.is_read
        })
        files_block = "\n".join(f"  - {f}" for f in staging_files) or "  (none)"
        base_prompt = PromptBuilder.build_section_prompt_from_clusters(
            "appendix", cluster_texts, self.ctx
        )
        extra = (
            f"\n\nAdditional appendix item — ETL Staging File Reference:\n"
            f"Include a sub-section listing all ETL staging files produced by the file version:\n"
            f"{files_block}\n"
            f"For each file: name, target table/dataset, operation type, and expected schema/columns."
        )
        return self.llm.query(base_prompt + extra, max_tokens=self._TOKENS_PER_SECTION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_dep_context(cobol_files: list[Path]) -> str:
    if not cobol_files:
        return ""
    analyses = [parse_file(p) for p in cobol_files]
    graph = build_dependency_graph(analyses)
    lines = []
    for src, deps in sorted(graph.items()):
        for dep in deps:
            lines.append(f"{src} --[{dep['dep_type']}]--> {dep['target']}")
    return "\n".join(lines)


def _clusters_block(cluster_texts: list[str]) -> str:
    return "\n\n".join(
        f"--- Cluster {i+1} ---\n{t}" for i, t in enumerate(cluster_texts)
    )
