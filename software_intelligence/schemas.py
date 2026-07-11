"""
Software Intelligence Platform — Shared Schemas
=================================================
Core domain objects shared across all OSIP modules.
Everything flows through these types — they are the platform's lingua franca.

Design principle: schemas are pure dataclasses (no framework coupling).
Pydantic variants are used only at the API boundary (interfaces/).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ── Repository ─────────────────────────────────────────────────────────────────

class ProviderType(str, Enum):
    GITHUB    = "github"
    GITLAB    = "gitlab"
    BITBUCKET = "bitbucket"
    GIT_LOCAL = "git_local"
    LOCAL_FS  = "local_fs"


class SyncStatus(str, Enum):
    PENDING   = "pending"
    SYNCING   = "syncing"
    COMPLETE  = "complete"
    FAILED    = "failed"
    STALE     = "stale"


@dataclass
class RepositoryRecord:
    """Authoritative identity record for one repository."""
    repo_id:         str
    full_name:       str                     # "owner/repo" or absolute path
    provider:        ProviderType
    default_branch:  str                     = "main"
    description:     str                     = ""
    primary_language: str                    = "unknown"
    languages:       dict[str, int]          = field(default_factory=dict)  # lang → bytes
    topics:          list[str]               = field(default_factory=list)
    stars:           int                     = 0
    forks:           int                     = 0
    size_kb:         int                     = 0
    last_commit_sha: str                     = ""
    last_synced_at:  str                     = ""
    sync_status:     SyncStatus              = SyncStatus.PENDING
    local_path:      str                     = ""
    is_monorepo:     bool                    = False
    tags:            dict[str, str]          = field(default_factory=dict)
    created_at:      str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SourceFile:
    """A single source file within a repository."""
    file_id:     str
    repo_id:     str
    path:        str                         # relative to repo root
    language:    str
    content:     str
    size_bytes:  int
    sha:         str                         = ""
    encoding:    str                         = "utf-8"
    is_binary:   bool                        = False
    is_test:     bool                        = False
    is_generated: bool                       = False
    last_modified: str                       = ""


@dataclass
class CommitRecord:
    sha:         str
    repo_id:     str
    message:     str
    author:      str
    email:       str
    timestamp:   str
    files_changed: list[str]                 = field(default_factory=list)
    insertions:  int                         = 0
    deletions:   int                         = 0
    parent_shas: list[str]                   = field(default_factory=list)


# ── Language / Parsing ─────────────────────────────────────────────────────────

class Language(str, Enum):
    PYTHON     = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    JAVA       = "java"
    CPP        = "cpp"
    C          = "c"
    GO         = "go"
    RUST       = "rust"
    RUBY       = "ruby"
    CSHARP     = "csharp"
    KOTLIN     = "kotlin"
    SWIFT      = "swift"
    SCALA      = "scala"
    MARKDOWN   = "markdown"
    YAML       = "yaml"
    JSON       = "json"
    UNKNOWN    = "unknown"


# ── AST Nodes ──────────────────────────────────────────────────────────────────

class NodeKind(str, Enum):
    MODULE     = "module"
    CLASS      = "class"
    FUNCTION   = "function"
    METHOD     = "method"
    IMPORT     = "import"
    VARIABLE   = "variable"
    INTERFACE  = "interface"
    ENUM       = "enum"
    DECORATOR  = "decorator"
    COMMENT    = "comment"
    BLOCK      = "block"


@dataclass
class ASTNode:
    """Language-agnostic AST node. All parsers emit these."""
    node_id:     str
    kind:        NodeKind
    name:        str
    file_path:   str
    line_start:  int
    line_end:    int
    language:    Language
    parent_id:   str | None               = None
    children:    list[str]                = field(default_factory=list)  # child node_ids
    docstring:   str                      = ""
    signature:   str                      = ""
    decorators:  list[str]               = field(default_factory=list)
    modifiers:   list[str]               = field(default_factory=list)  # public/static/async
    metadata:    dict[str, Any]          = field(default_factory=dict)


@dataclass
class FunctionNode(ASTNode):
    parameters:      list[str]           = field(default_factory=list)
    return_type:     str                 = ""
    is_async:        bool                = False
    is_generator:    bool                = False
    cyclomatic_complexity: int           = 1
    cognitive_complexity: int            = 0
    calls:           list[str]           = field(default_factory=list)  # called function names


@dataclass
class ClassNode(ASTNode):
    bases:           list[str]           = field(default_factory=list)  # parent class names
    interfaces:      list[str]           = field(default_factory=list)
    methods:         list[str]           = field(default_factory=list)  # method node_ids
    class_variables: list[str]           = field(default_factory=list)
    is_abstract:     bool                = False
    is_dataclass:    bool                = False


@dataclass
class ImportNode(ASTNode):
    module:          str                 = ""
    symbols:         list[str]           = field(default_factory=list)
    alias:           str                 = ""
    is_relative:     bool                = False
    is_stdlib:       bool                = False
    is_external:     bool                = False


@dataclass
class ParseResult:
    """Everything extracted from one source file."""
    file_id:     str
    file_path:   str
    language:    Language
    nodes:       list[ASTNode]           = field(default_factory=list)
    imports:     list[ImportNode]        = field(default_factory=list)
    functions:   list[FunctionNode]      = field(default_factory=list)
    classes:     list[ClassNode]         = field(default_factory=list)
    line_count:  int                     = 0
    token_count: int                     = 0
    errors:      list[str]              = field(default_factory=list)
    raw_ast:     Any                     = None       # framework-specific AST (Python ast.Module, etc.)


# ── Dependency ─────────────────────────────────────────────────────────────────

class DependencyKind(str, Enum):
    INTERNAL   = "internal"     # within the repo
    STDLIB     = "stdlib"       # language standard library
    EXTERNAL   = "external"     # third-party package
    PEER       = "peer"         # another repo / service


@dataclass
class Dependency:
    source:      str             # file_path or module name
    target:      str             # file_path or package name
    kind:        DependencyKind
    symbol:      str             = ""   # specific imported symbol
    is_circular: bool            = False
    is_unused:   bool            = False
    line:        int             = 0


@dataclass
class DependencyGraph:
    repo_id:     str
    nodes:       list[str]                    = field(default_factory=list)   # module paths
    edges:       list[Dependency]             = field(default_factory=list)
    circular:    list[list[str]]              = field(default_factory=list)   # cycles
    unused:      list[str]                    = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Architecture ───────────────────────────────────────────────────────────────

class LayerType(str, Enum):
    PRESENTATION = "presentation"
    API          = "api"
    APPLICATION  = "application"
    DOMAIN       = "domain"
    INFRASTRUCTURE = "infrastructure"
    DATA         = "data"
    UTILITY      = "utility"
    CONFIGURATION = "configuration"
    TEST         = "test"
    UNKNOWN      = "unknown"


@dataclass
class ArchitecturalComponent:
    name:        str
    layer:       LayerType
    paths:       list[str]               = field(default_factory=list)
    entry_points: list[str]              = field(default_factory=list)
    outbound:    list[str]               = field(default_factory=list)  # components this calls
    inbound:     list[str]               = field(default_factory=list)  # components that call this
    description: str                     = ""


@dataclass
class ArchitectureReport:
    repo_id:     str
    components:  list[ArchitecturalComponent] = field(default_factory=list)
    entry_points: list[str]              = field(default_factory=list)
    boundaries:  list[str]               = field(default_factory=list)
    patterns:    list[str]               = field(default_factory=list)  # detected: MVC, layered, microservice
    smells:      list[str]               = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Metrics ────────────────────────────────────────────────────────────────────

@dataclass
class FileMetrics:
    file_path:              str
    language:               Language
    loc:                    int          = 0    # lines of code (non-blank, non-comment)
    sloc:                   int          = 0    # source lines
    cloc:                   int          = 0    # comment lines
    blank_lines:            int          = 0
    cyclomatic_complexity:  float        = 0.0
    cognitive_complexity:   float        = 0.0
    maintainability_index:  float        = 0.0  # 0–100 (higher = more maintainable)
    fan_in:                 int          = 0    # modules importing this
    fan_out:                int          = 0    # modules this imports
    coupling:               float        = 0.0  # 0–1
    cohesion:               float        = 0.0  # 0–1 (higher = better)
    num_functions:          int          = 0
    num_classes:            int          = 0
    avg_function_length:    float        = 0.0
    max_function_length:    int          = 0
    comment_ratio:          float        = 0.0
    docstring_coverage:     float        = 0.0
    duplication_ratio:      float        = 0.0


@dataclass
class RepositoryMetrics:
    repo_id:                str
    total_loc:              int          = 0
    total_files:            int          = 0
    total_functions:        int          = 0
    total_classes:          int          = 0
    avg_cyclomatic:         float        = 0.0
    avg_maintainability:    float        = 0.0
    avg_coupling:           float        = 0.0
    avg_cohesion:           float        = 0.0
    test_coverage_ratio:    float        = 0.0   # test files / total files
    doc_coverage:           float        = 0.0
    language_breakdown:     dict[str, int] = field(default_factory=dict)  # lang → LOC
    file_metrics:           list[FileMetrics] = field(default_factory=list)
    computed_at:            str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Technical Debt ─────────────────────────────────────────────────────────────

class DebtCategory(str, Enum):
    COMPLEXITY    = "complexity"
    DUPLICATION   = "duplication"
    DEAD_CODE     = "dead_code"
    LARGE_FILE    = "large_file"
    LONG_FUNCTION = "long_function"
    ARCH_SMELL    = "architectural_smell"
    MISSING_TESTS = "missing_tests"
    POOR_NAMING   = "poor_naming"
    SECURITY      = "security"
    DOCUMENTATION = "documentation"


class DebtSeverity(str, Enum):
    INFO     = "info"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


@dataclass
class DebtItem:
    debt_id:     str
    category:    DebtCategory
    severity:    DebtSeverity
    file_path:   str
    line_start:  int             = 0
    line_end:    int             = 0
    title:       str             = ""
    description: str             = ""
    remediation: str             = ""
    effort_minutes: int          = 0   # estimated fix time
    tags:        list[str]       = field(default_factory=list)


@dataclass
class TechnicalDebtReport:
    repo_id:        str
    items:          list[DebtItem]        = field(default_factory=list)
    total_effort_h: float                 = 0.0
    debt_score:     float                 = 0.0   # 0–100 (higher = more debt)
    by_category:    dict[str, int]        = field(default_factory=dict)
    by_severity:    dict[str, int]        = field(default_factory=dict)
    hotspots:       list[str]             = field(default_factory=list)  # most indebted files
    generated_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Security ───────────────────────────────────────────────────────────────────

class SecuritySeverity(str, Enum):
    INFO     = "info"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class SecurityFindingKind(str, Enum):
    HARDCODED_SECRET    = "hardcoded_secret"
    CREDENTIAL_EXPOSURE = "credential_exposure"
    UNSAFE_PATTERN      = "unsafe_pattern"
    DEPENDENCY_VULN     = "dependency_vulnerability"
    CONFIG_MISTAKE      = "configuration_mistake"
    INJECTION_RISK      = "injection_risk"
    CRYPTO_WEAKNESS     = "cryptographic_weakness"
    PATH_TRAVERSAL      = "path_traversal"
    SAST_FINDING        = "sast_finding"


@dataclass
class SecurityFinding:
    finding_id:  str
    kind:        SecurityFindingKind
    severity:    SecuritySeverity
    file_path:   str
    line:        int                 = 0
    snippet:     str                 = ""
    title:       str                 = ""
    description: str                 = ""
    remediation: str                 = ""
    cwe:         str                 = ""   # CWE-ID
    cve:         str                 = ""   # CVE-ID (for dep vulns)
    confidence:  float               = 0.0  # 0–1


@dataclass
class SecurityReport:
    repo_id:        str
    findings:       list[SecurityFinding]  = field(default_factory=list)
    risk_score:     float                  = 0.0
    by_severity:    dict[str, int]         = field(default_factory=dict)
    by_kind:        dict[str, int]         = field(default_factory=dict)
    scanned_files:  int                    = 0
    generated_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Issue / PR ─────────────────────────────────────────────────────────────────

class IssuePriority(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    UNKNOWN  = "unknown"


@dataclass
class IssueRecord:
    issue_id:    str
    repo_id:     str
    number:      int
    title:       str
    body:        str
    state:       str                  # open / closed
    labels:      list[str]           = field(default_factory=list)
    author:      str                  = ""
    assignees:   list[str]           = field(default_factory=list)
    created_at:  str                  = ""
    updated_at:  str                  = ""
    closed_at:   str                  = ""
    url:         str                  = ""


@dataclass
class IssueAnalysis:
    issue_id:    str
    summary:     str                  = ""
    category:    str                  = ""    # bug / feature / enhancement / docs
    priority:    IssuePriority        = IssuePriority.UNKNOWN
    duplicate_of: str | None          = None  # issue_id of canonical
    similarity_score: float           = 0.0
    affected_components: list[str]   = field(default_factory=list)
    suggested_labels:    list[str]   = field(default_factory=list)
    effort_estimate:     str          = ""


@dataclass
class PullRequestRecord:
    pr_id:       str
    repo_id:     str
    number:      int
    title:       str
    body:        str
    state:       str                  # open / merged / closed
    base_branch: str                  = "main"
    head_branch: str                  = ""
    author:      str                  = ""
    reviewers:   list[str]           = field(default_factory=list)
    labels:      list[str]           = field(default_factory=list)
    files_changed: list[str]         = field(default_factory=list)
    additions:   int                  = 0
    deletions:   int                  = 0
    created_at:  str                  = ""
    merged_at:   str                  = ""
    url:         str                  = ""


@dataclass
class PRAnalysis:
    pr_id:       str
    summary:     str                  = ""
    risk_score:  float                = 0.0   # 0–1 (higher = riskier)
    risk_factors: list[str]          = field(default_factory=list)
    affected_components: list[str]   = field(default_factory=list)
    suggested_reviewers: list[str]   = field(default_factory=list)
    review_checklist:    list[str]   = field(default_factory=list)
    breaking_changes:    bool         = False
    requires_migration:  bool         = False
    test_coverage_delta: float        = 0.0


# ── Knowledge Graph ────────────────────────────────────────────────────────────

class GraphNodeKind(str, Enum):
    REPOSITORY = "repository"
    MODULE     = "module"
    CLASS      = "class"
    FUNCTION   = "function"
    DEPENDENCY = "dependency"
    ISSUE      = "issue"
    PR         = "pull_request"
    AUTHOR     = "author"
    CONCEPT    = "concept"


class GraphEdgeKind(str, Enum):
    IMPORTS    = "imports"
    CALLS      = "calls"
    INHERITS   = "inherits"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    MODIFIES   = "modifies"
    CONTAINS   = "contains"
    AUTHORED   = "authored"
    FIXES      = "fixes"
    DEPENDS_ON = "depends_on"
    SIMILAR_TO = "similar_to"


@dataclass
class GraphNode:
    node_id:   str
    kind:      GraphNodeKind
    name:      str
    repo_id:   str
    properties: dict[str, Any]         = field(default_factory=dict)


@dataclass
class GraphEdge:
    edge_id:   str
    source_id: str
    target_id: str
    kind:      GraphEdgeKind
    weight:    float                   = 1.0
    properties: dict[str, Any]        = field(default_factory=dict)


# ── Review ─────────────────────────────────────────────────────────────────────

class ReviewSeverity(str, Enum):
    INFO     = "info"
    SUGGEST  = "suggest"
    WARNING  = "warning"
    ERROR    = "error"


class ReviewCategory(str, Enum):
    QUALITY        = "code_quality"
    MAINTAINABILITY = "maintainability"
    NAMING         = "naming"
    ARCHITECTURE   = "architecture"
    SECURITY       = "security"
    PERFORMANCE    = "performance"
    BEST_PRACTICE  = "best_practice"
    DOCUMENTATION  = "documentation"
    TEST_COVERAGE  = "test_coverage"


@dataclass
class ReviewComment:
    comment_id:  str
    category:    ReviewCategory
    severity:    ReviewSeverity
    file_path:   str
    line_start:  int               = 0
    line_end:    int               = 0
    title:       str               = ""
    message:     str               = ""
    suggestion:  str               = ""   # proposed fix / code snippet


@dataclass
class CodeReviewReport:
    review_id:   str
    repo_id:     str
    pr_id:       str                  = ""
    comments:    list[ReviewComment] = field(default_factory=list)
    score:       float               = 0.0   # 0–100 (higher = better)
    by_category: dict[str, int]     = field(default_factory=dict)
    summary:     str                 = ""
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Search ─────────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    result_id:   str
    kind:        str                  # "file", "function", "class", "doc", "issue"
    repo_id:     str
    path:        str
    name:        str
    snippet:     str
    score:       float               = 0.0
    line:        int                 = 0
    metadata:    dict[str, Any]     = field(default_factory=dict)


# ── Pipeline ───────────────────────────────────────────────────────────────────

class ProcessingStage(str, Enum):
    INGEST       = "ingest"
    PARSE        = "parse"
    ANALYZE      = "analyze"
    DEPENDENCY   = "dependency"
    ARCHITECTURE = "architecture"
    METRICS      = "metrics"
    SECURITY     = "security"
    DEBT         = "technical_debt"
    KNOWLEDGE    = "knowledge_graph"
    EMBED        = "embed"
    REPORT       = "report"


@dataclass
class ProcessingJob:
    job_id:      str
    repo_id:     str
    stages:      list[ProcessingStage]   = field(default_factory=list)
    status:      str                      = "pending"
    current_stage: str                    = ""
    progress:    float                    = 0.0
    errors:      list[str]               = field(default_factory=list)
    outputs:     dict[str, Any]          = field(default_factory=dict)
    created_at:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str                    = ""
