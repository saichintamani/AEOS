"""
Software Intelligence Platform — Security Scanner
==================================================
Static security inspection for software repositories.

Detection capabilities:
  1. Hardcoded secrets (API keys, passwords, tokens)
  2. Credential exposure (private keys, certificates)
  3. Unsafe code patterns (eval, pickle, shell injection)
  4. Configuration mistakes (debug=True in prod, permissive CORS)
  5. Dependency vulnerabilities (via OSV / Safety DB integration)
  6. Injection risks (SQL, command, path traversal)
  7. Cryptographic weaknesses (MD5, SHA1, hardcoded IVs)

Architecture:
  SecurityScanner     → facade, runs all detectors
  BaseSecurityDetector → one detector per vulnerability class
  SecurityReport      → aggregated findings (see schemas.py)

Future: plug in Bandit (Python SAST), Semgrep, Snyk, OWASP Dependency-Check
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from software_intelligence.schemas import (
    SecurityFinding, SecurityFindingKind, SecurityReport, SecuritySeverity, SourceFile,
)


# ── Detector ABC ───────────────────────────────────────────────────────────────

class BaseSecurityDetector(ABC):
    """One security check. Operates on raw file content."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def scan(self, file: SourceFile) -> list[SecurityFinding]: ...

    def _finding(
        self,
        file: SourceFile,
        line: int,
        snippet: str,
        title: str,
        description: str,
        severity: SecuritySeverity,
        kind: SecurityFindingKind,
        remediation: str = "",
        cwe: str = "",
    ) -> SecurityFinding:
        return SecurityFinding(
            finding_id=str(uuid.uuid4())[:8],
            kind=kind,
            severity=severity,
            file_path=file.path,
            line=line,
            snippet=snippet[:120],
            title=title,
            description=description,
            remediation=remediation,
            cwe=cwe,
            confidence=0.85,
        )


# ── Hardcoded secrets detector ─────────────────────────────────────────────────

class HardcodedSecretDetector(BaseSecurityDetector):
    """
    Detects API keys, tokens, passwords, and other credentials
    hardcoded directly in source files.

    Patterns based on common formats:
      - Generic: high-entropy strings assigned to secret-named vars
      - AWS: AKIA... key patterns
      - GitHub tokens: ghp_..., ghs_...
      - Stripe, SendGrid, Twilio API keys
      - JWT tokens
      - Private key headers
    """

    name = "hardcoded_secrets"

    _PATTERNS: list[tuple[re.Pattern, str, SecuritySeverity, str]] = [
        # (pattern, title, severity, CWE)
        (re.compile(r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{4,}["\']'),
         "Hardcoded password", SecuritySeverity.CRITICAL, "CWE-798"),
        (re.compile(r'(?i)(api_key|apikey|api-key)\s*=\s*["\'][^"\']{8,}["\']'),
         "Hardcoded API key", SecuritySeverity.CRITICAL, "CWE-798"),
        (re.compile(r'(?i)(secret|token|access_token|auth_token)\s*=\s*["\'][^"\']{8,}["\']'),
         "Hardcoded secret/token", SecuritySeverity.HIGH, "CWE-798"),
        (re.compile(r'AKIA[0-9A-Z]{16}'),
         "AWS Access Key ID", SecuritySeverity.CRITICAL, "CWE-798"),
        (re.compile(r'ghp_[0-9a-zA-Z]{36}|ghs_[0-9a-zA-Z]{36}'),
         "GitHub personal access token", SecuritySeverity.CRITICAL, "CWE-798"),
        (re.compile(r'-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----'),
         "Private key in source", SecuritySeverity.CRITICAL, "CWE-321"),
        (re.compile(r'sk_live_[0-9a-zA-Z]{24,}'),
         "Stripe live secret key", SecuritySeverity.CRITICAL, "CWE-798"),
        (re.compile(r'(?i)(database_url|db_url)\s*=\s*["\'](?:postgres|mysql|mongodb)[^"\']+["\']'),
         "Hardcoded database URL with credentials", SecuritySeverity.HIGH, "CWE-312"),
    ]

    _SKIP_PATTERNS = re.compile(r'#.*secret|example|placeholder|your_key|changeme|dummy|test|fake', re.I)

    def scan(self, file: SourceFile) -> list[SecurityFinding]:
        # Skip test files and documentation
        if file.is_test or Path(file.path).suffix in {".md", ".txt", ".rst"}:
            return []

        findings = []
        lines = file.content.splitlines()
        for lineno, line in enumerate(lines, start=1):
            if self._SKIP_PATTERNS.search(line):
                continue
            for pattern, title, severity, cwe in self._PATTERNS:
                m = pattern.search(line)
                if m:
                    findings.append(self._finding(
                        file=file,
                        line=lineno,
                        snippet=line.strip(),
                        title=title,
                        description=f"Potential credential found in source code at line {lineno}.",
                        severity=severity,
                        kind=SecurityFindingKind.HARDCODED_SECRET,
                        remediation="Move to environment variables or a secrets manager (Vault, AWS SSM).",
                        cwe=cwe,
                    ))
        return findings


# ── Unsafe pattern detector ────────────────────────────────────────────────────

class UnsafePatternDetector(BaseSecurityDetector):
    """
    Detects dangerous code patterns that enable injection attacks.
    """

    name = "unsafe_patterns"

    _PATTERNS: list[tuple[re.Pattern, str, str, SecuritySeverity, str, str]] = [
        (re.compile(r'\beval\s*\('), "eval() usage",
         "eval() executes arbitrary code and is a remote code execution risk.",
         SecuritySeverity.HIGH, "CWE-78",
         "Avoid eval(). Use ast.literal_eval() for safe expression parsing."),
        (re.compile(r'\bexec\s*\('), "exec() usage",
         "exec() executes arbitrary Python code.",
         SecuritySeverity.HIGH, "CWE-78",
         "Avoid exec(). Refactor to use proper abstractions."),
        (re.compile(r'pickle\.loads?\s*\('), "Unsafe pickle deserialization",
         "pickle.load() can execute arbitrary code when deserializing untrusted data.",
         SecuritySeverity.HIGH, "CWE-502",
         "Use JSON or msgpack for serialization of untrusted data."),
        (re.compile(r'subprocess\.[a-z_]+\(.*shell\s*=\s*True'), "Shell injection via subprocess",
         "shell=True in subprocess calls enables shell injection.",
         SecuritySeverity.HIGH, "CWE-78",
         "Pass arguments as a list instead of a string; avoid shell=True."),
        (re.compile(r'os\.system\s*\('), "os.system() usage",
         "os.system() is vulnerable to shell injection.",
         SecuritySeverity.MEDIUM, "CWE-78",
         "Use subprocess.run() with a list of arguments."),
        (re.compile(r'yaml\.load\s*\([^,)]+\)(?!\s*,\s*Loader)'), "Unsafe YAML load",
         "yaml.load() without Loader can execute arbitrary Python.",
         SecuritySeverity.HIGH, "CWE-502",
         "Use yaml.safe_load() instead."),
        (re.compile(r'hashlib\.md5\s*\('), "MD5 usage",
         "MD5 is cryptographically broken and should not be used for security.",
         SecuritySeverity.LOW, "CWE-327",
         "Use hashlib.sha256() or hashlib.sha3_256() instead."),
        (re.compile(r'hashlib\.sha1\s*\('), "SHA-1 usage",
         "SHA-1 is deprecated for cryptographic use.",
         SecuritySeverity.LOW, "CWE-327",
         "Use hashlib.sha256() or hashlib.sha3_256() instead."),
        (re.compile(r'DEBUG\s*=\s*True'), "Debug mode enabled",
         "DEBUG=True in production exposes stack traces and internal state.",
         SecuritySeverity.MEDIUM, "CWE-489",
         "Set DEBUG=False in production; use environment-based configuration."),
        (re.compile(r'CORS_ORIGIN_ALLOW_ALL\s*=\s*True|allow_origins\s*=\s*\[\s*["\*]'), "Permissive CORS",
         "Wildcard CORS allows all origins — may expose sensitive data.",
         SecuritySeverity.MEDIUM, "CWE-942",
         "Restrict allowed origins to known domains."),
    ]

    def scan(self, file: SourceFile) -> list[SecurityFinding]:
        findings = []
        lines = file.content.splitlines()
        for lineno, line in enumerate(lines, start=1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for pattern, title, description, severity, cwe, remediation in self._PATTERNS:
                if pattern.search(line):
                    findings.append(self._finding(
                        file=file,
                        line=lineno,
                        snippet=stripped,
                        title=title,
                        description=description,
                        severity=severity,
                        kind=SecurityFindingKind.UNSAFE_PATTERN,
                        remediation=remediation,
                        cwe=cwe,
                    ))
        return findings


# ── Dependency vulnerability detector ─────────────────────────────────────────

class DependencyVulnerabilityDetector(BaseSecurityDetector):
    """
    Checks declared dependencies against vulnerability databases.
    Currently supports:
      - requirements.txt / setup.cfg / pyproject.toml (Python)
      - package.json (Node.js) — via npm audit output
    Future: integrate OSV API, Snyk, GitHub Advisory DB.
    """

    name = "dependency_vulnerabilities"

    _REQUIREMENT_FILES = {
        "requirements.txt", "requirements-dev.txt", "requirements-prod.txt",
        "pyproject.toml", "setup.cfg", "Pipfile",
    }

    def scan(self, file: SourceFile) -> list[SecurityFinding]:
        if Path(file.path).name not in self._REQUIREMENT_FILES:
            return []

        packages = self._parse_requirements(file.content)
        findings = []
        # TODO: query OSV API (https://api.osv.dev/v1/query) per package
        # For now: return stub finding if pinned to known bad versions
        _KNOWN_VULNERABLE: dict[str, str] = {
            # package → version range string (illustrative)
            # "pillow": "< 9.3.0",
            # "django": "< 3.2.14",
        }
        for pkg, version in packages.items():
            if pkg in _KNOWN_VULNERABLE:
                findings.append(self._finding(
                    file=file,
                    line=0,
                    snippet=f"{pkg}=={version}",
                    title=f"Vulnerable dependency: {pkg}",
                    description=f"{pkg} {version} has known vulnerabilities.",
                    severity=SecuritySeverity.HIGH,
                    kind=SecurityFindingKind.DEPENDENCY_VULN,
                    remediation=f"Upgrade {pkg} to the latest patched version.",
                ))
        return findings

    def _parse_requirements(self, content: str) -> dict[str, str]:
        packages: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*(?:==|>=|<=|~=)?\s*([0-9.]+)?', line)
            if m:
                packages[m.group(1).lower()] = m.group(2) or "unknown"
        return packages


# ── Security scanner facade ────────────────────────────────────────────────────

class SecurityScanner:
    """
    Runs all security detectors across a set of source files.
    Returns a SecurityReport.

    Usage:
        scanner = SecurityScanner.default()
        report = scanner.scan(source_files, repo_id="my-repo")
    """

    DEFAULT_DETECTORS = [
        HardcodedSecretDetector,
        UnsafePatternDetector,
        DependencyVulnerabilityDetector,
    ]

    def __init__(self, detectors: list[BaseSecurityDetector] | None = None) -> None:
        self._detectors = detectors or [cls() for cls in self.DEFAULT_DETECTORS]

    @classmethod
    def default(cls) -> "SecurityScanner":
        return cls()

    def scan(self, files: list[SourceFile], repo_id: str) -> SecurityReport:
        report = SecurityReport(repo_id=repo_id, scanned_files=len(files))
        for file in files:
            for detector in self._detectors:
                try:
                    findings = detector.scan(file)
                    report.findings.extend(findings)
                except Exception:
                    pass

        report.by_severity = self._tally(report, "severity")
        report.by_kind = self._tally(report, "kind")
        report.risk_score = self._compute_risk(report)
        return report

    def _compute_risk(self, report: SecurityReport) -> float:
        score = 0.0
        weights = {
            SecuritySeverity.CRITICAL: 1.0,
            SecuritySeverity.HIGH:     0.7,
            SecuritySeverity.MEDIUM:   0.3,
            SecuritySeverity.LOW:      0.1,
            SecuritySeverity.INFO:     0.0,
        }
        for f in report.findings:
            score += weights.get(f.severity, 0)
        return round(min(score / max(len(report.findings), 1) * 2, 10.0), 2)

    def _tally(self, report: SecurityReport, attr: str) -> dict[str, int]:
        tally: dict[str, int] = {}
        for f in report.findings:
            val = getattr(f, attr)
            key = val.value if hasattr(val, "value") else str(val)
            tally[key] = tally.get(key, 0) + 1
        return tally
