"""
AEOS CLI — Phase 10 Developer Platform

Entry point: `aeos` command installed via pyproject.toml [project.scripts].

Commands:
  aeos init [template]          Scaffold a new AEOS project
  aeos start                    Start the AEOS runtime (API server)
  aeos cluster status           Show 3-node cluster state
  aeos cluster health           Hit /health on all nodes
  aeos cluster start            Start the cluster via docker-compose
  aeos cluster stop             Stop the cluster
  aeos workflow submit          Submit a task/workflow
  aeos workflow inspect         Inspect a running workflow
  aeos workflow list            List recent workflows
  aeos benchmark                Run the performance benchmark suite
  aeos validate                 Trigger invariant engine evaluation
  aeos version                  Print AEOS version

Requires: typer (pip install typer[all])
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    import typer
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    _RICH_AVAILABLE = True
except ImportError:
    typer = None  # type: ignore
    _RICH_AVAILABLE = False

if typer is None:
    print("AEOS CLI requires typer and rich: pip install typer[all] rich")
    sys.exit(1)

app = typer.Typer(
    name="aeos",
    help="AEOS — AI Engineering Orchestration System CLI",
    add_completion=True,
    rich_markup_mode="rich",
)

cluster_app = typer.Typer(help="Manage the AEOS distributed cluster")
workflow_app = typer.Typer(help="Submit and inspect workflows")
app.add_typer(cluster_app, name="cluster")
app.add_typer(workflow_app, name="workflow")

console = Console()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_project_root() -> Path:
    """Walk up until we find pyproject.toml or docker-compose.yml."""
    here = Path.cwd()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists() or (parent / "docker-compose.yml").exists():
            return parent
    return here


def _api_get(path: str, host: str = "http://localhost:8000") -> dict:
    try:
        import urllib.request
        url = f"{host}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


def _api_post(path: str, body: dict, host: str = "http://localhost:8000") -> dict:
    try:
        import urllib.request, urllib.error
        import json as _json
        data = _json.dumps(body).encode()
        req = urllib.request.Request(
            f"{host}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


# ── aeos version ──────────────────────────────────────────────────────────────

@app.command("version")
def cmd_version():
    """Print the AEOS version."""
    from aeos import __version__
    console.print(f"[bold cyan]AEOS[/bold cyan] v{__version__}")


# ── aeos init ─────────────────────────────────────────────────────────────────

TEMPLATES = {
    "research-assistant": "Research workflow with RAG and multi-agent reasoning",
    "rag-system":         "Pure RAG ingestion + query API",
    "multi-agent":        "Multi-agent collaboration with planner, researcher, reviewer",
    "minimal":            "Bare-bones single-agent setup",
}

@app.command("init")
def cmd_init(
    template: str = typer.Argument(default="minimal", help=f"Project template: {', '.join(TEMPLATES)}"),
    name: str = typer.Option("my-aeos-project", "--name", "-n", help="Project directory name"),
):
    """Scaffold a new AEOS project from a template."""
    if template not in TEMPLATES:
        console.print(f"[red]Unknown template '{template}'.[/red]")
        console.print("Available templates:")
        for t, desc in TEMPLATES.items():
            console.print(f"  [cyan]{t}[/cyan]  — {desc}")
        raise typer.Exit(1)

    target = Path.cwd() / name
    if target.exists():
        console.print(f"[red]Directory '{name}' already exists.[/red]")
        raise typer.Exit(1)

    console.print(f"Scaffolding [bold cyan]{template}[/bold cyan] into [bold]{name}/[/bold] ...")

    from aeos.cli.scaffold import create_project
    create_project(template=template, target=target)

    console.print(Panel(
        f"[green]✓ Project created at {target}[/green]\n\n"
        f"  [bold]cd {name}[/bold]\n"
        f"  [bold]aeos start[/bold]     — start the runtime\n"
        f"  [bold]aeos workflow submit --task 'Your task here'[/bold]",
        title="[bold cyan]AEOS Project Ready[/bold cyan]",
    ))


# ── aeos start ────────────────────────────────────────────────────────────────

@app.command("start")
def cmd_start(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Hot reload (development mode)"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of uvicorn worker processes"),
):
    """Start the AEOS runtime API server."""
    console.print(Panel(
        f"[bold cyan]AEOS Runtime[/bold cyan]\n"
        f"  API:      http://{host}:{port}\n"
        f"  Docs:     http://{host}:{port}/api/v1/docs\n"
        f"  Metrics:  http://{host}:{port}/metrics",
        title="Starting AEOS",
    ))
    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", host,
        "--port", str(port),
        "--workers", str(workers),
    ]
    if reload:
        cmd.append("--reload")
    os.execv(sys.executable, cmd)


# ── aeos cluster ──────────────────────────────────────────────────────────────

_CLUSTER_NODES = [
    ("node-1", "http://localhost:8001"),
    ("node-2", "http://localhost:8002"),
    ("node-3", "http://localhost:8003"),
]

@cluster_app.command("status")
def cluster_status():
    """Show the current state of all cluster nodes."""
    table = Table(title="AEOS Cluster Status", show_header=True, header_style="bold cyan")
    table.add_column("Node",    style="bold")
    table.add_column("URL")
    table.add_column("Status")
    table.add_column("Version")
    table.add_column("Kernel")

    for node_id, url in _CLUSTER_NODES:
        result = _api_get("/health", host=url)
        if "error" in result:
            table.add_row(node_id, url, "[red]unreachable[/red]", "-", "-")
        else:
            status_color = "green" if result.get("status") == "healthy" else "yellow"
            table.add_row(
                node_id,
                url,
                f"[{status_color}]{result.get('status', '?')}[/{status_color}]",
                result.get("version", "?"),
                result.get("environment", "?"),
            )
    console.print(table)


@cluster_app.command("health")
def cluster_health():
    """Run health + invariant checks on all nodes."""
    for node_id, url in _CLUSTER_NODES:
        health = _api_get("/health", host=url)
        inv = _api_get("/api/v1/validation/status", host=url)

        h_status = health.get("status", "unreachable")
        h_color = "green" if h_status == "healthy" else "red"

        v_status = inv.get("status", "unavailable")
        v_stats = inv.get("stats", {})
        violations = v_stats.get("total_violations", "?")

        console.print(
            f"[bold]{node_id}[/bold] ({url}): "
            f"health=[{h_color}]{h_status}[/{h_color}]  "
            f"invariant-monitor={v_status}  violations={violations}"
        )


@cluster_app.command("start")
def cluster_start(
    monitor: bool = typer.Option(False, "--monitor", "-m", help="Include Prometheus/Grafana/Jaeger"),
):
    """Start the 3-node cluster via docker-compose."""
    root = _find_project_root()
    compose_file = root / "docker-compose.cluster.yml"
    if not compose_file.exists():
        console.print(f"[red]docker-compose.cluster.yml not found in {root}[/red]")
        raise typer.Exit(1)

    cmd = ["docker-compose", "-f", str(compose_file)]
    if monitor:
        cmd += ["--profile", "monitoring"]
    cmd += ["up", "-d"]

    console.print(f"Starting cluster from [bold]{compose_file.name}[/bold] ...")
    subprocess.run(cmd, check=True)
    console.print("[green]Cluster started.[/green] Run [bold]aeos cluster health[/bold] to verify.")


@cluster_app.command("stop")
def cluster_stop():
    """Stop and remove the cluster containers."""
    root = _find_project_root()
    compose_file = root / "docker-compose.cluster.yml"
    subprocess.run(
        ["docker-compose", "-f", str(compose_file), "--profile", "monitoring", "down"],
        check=True,
    )
    console.print("[green]Cluster stopped.[/green]")


# ── aeos workflow ─────────────────────────────────────────────────────────────

@workflow_app.command("submit")
def workflow_submit(
    task: str = typer.Option(..., "--task", "-t", help="Task description to submit"),
    mode: str = typer.Option("single-agent", "--mode", "-m", help="'single-agent' | 'multi-agent'"),
    host: str = typer.Option("http://localhost:8000", "--host", help="AEOS API host"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="YAML workflow definition file"),
):
    """Submit a task or YAML workflow to the AEOS runtime."""
    if file:
        _submit_yaml_workflow(file, host)
        return

    console.print(f"Submitting task: [italic]{task[:80]}...[/italic]" if len(task) > 80 else f"Submitting: [italic]{task}[/italic]")
    result = _api_post("/api/v1/run", {"task": task, "mode": mode}, host=host)

    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        raise typer.Exit(1)

    status = result.get("status", "?")
    color = "green" if status == "success" else "yellow"
    console.print(f"Status: [{color}]{status}[/{color}]")

    agent = result.get("agent_id", result.get("agent", "?"))
    console.print(f"Agent:  {agent}")

    if result.get("result"):
        console.print(Panel(str(result["result"])[:800], title="Result"))
    elif result.get("response"):
        console.print(Panel(str(result["response"])[:800], title="Response"))


def _submit_yaml_workflow(file: Path, host: str) -> None:
    """Compile and submit a YAML workflow definition."""
    try:
        import yaml  # type: ignore
    except ImportError:
        console.print("[red]PyYAML required for YAML workflows: pip install pyyaml[/red]")
        raise typer.Exit(1)

    from aeos.workflow.compiler import WorkflowCompiler
    raw = yaml.safe_load(file.read_text())
    compiler = WorkflowCompiler()
    compiled = compiler.compile(raw)

    console.print(f"Compiled workflow: [bold]{compiled['name']}[/bold] ({len(compiled['steps'])} steps)")
    # Submit each step's task sequentially (simple execution model for now)
    for i, step in enumerate(compiled["steps"]):
        console.print(f"  Step {i+1}: {step['task'][:60]}")
        result = _api_post("/api/v1/run", {"task": step["task"], "mode": step.get("mode", "single-agent")}, host=host)
        status = result.get("status", "?")
        color = "green" if status == "success" else "red"
        console.print(f"         [{color}]{status}[/{color}]")


@workflow_app.command("inspect")
def workflow_inspect(
    host: str = typer.Option("http://localhost:8000", "--host", help="AEOS API host"),
):
    """Inspect the current execution engine state and last graph."""
    result = _api_get("/api/v1/execution/introspect", host=host)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise typer.Exit(1)

    table = Table(title="Execution Engine State", show_header=True, header_style="bold cyan")
    table.add_column("Property")
    table.add_column("Value")
    for k, v in result.items():
        if not isinstance(v, dict):
            table.add_row(k, str(v))
    console.print(table)

    metrics = _api_get("/api/v1/execution/metrics", host=host)
    if "summary" in metrics:
        console.print(Panel(json.dumps(metrics["summary"], indent=2), title="Metrics Summary"))


@workflow_app.command("list")
def workflow_list(
    host: str = typer.Option("http://localhost:8000", "--host", help="AEOS API host"),
):
    """List recent workflow executions from the execution engine."""
    result = _api_get("/api/v1/execution/metrics", host=host)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise typer.Exit(1)
    nodes = result.get("nodes", [])
    if not nodes:
        console.print("No execution records found.")
        return
    table = Table(title="Recent Workflow Nodes", show_header=True, header_style="bold cyan")
    table.add_column("Node ID")
    table.add_column("Executions")
    table.add_column("Success Rate")
    table.add_column("p50 (ms)")
    table.add_column("p95 (ms)")
    for n in nodes[:20]:
        table.add_row(
            n.get("node_id", "?"),
            str(n.get("execution_count", 0)),
            f"{n.get('success_rate', 0)*100:.0f}%",
            f"{n.get('p50_latency_ms', 0):.1f}",
            f"{n.get('p95_latency_ms', 0):.1f}",
        )
    console.print(table)


# ── aeos benchmark ────────────────────────────────────────────────────────────

@app.command("benchmark")
def cmd_benchmark(
    mode: str = typer.Option("local", "--mode", "-m", help="'local' | 'http'"),
    scale: str = typer.Option("100,1000", "--scale", "-s", help="Comma-separated workflow counts"),
    concurrency: int = typer.Option(20, "--concurrency", "-c", help="Max concurrent tasks"),
    host: str = typer.Option("http://localhost:8000", "--host", help="API host (http mode only)"),
):
    """Run the AEOS performance benchmark suite."""
    root = _find_project_root()
    bench_script = root / "scripts" / "benchmark.py"
    if not bench_script.exists():
        console.print("[red]scripts/benchmark.py not found. Run from the project root.[/red]")
        raise typer.Exit(1)

    cmd = [
        sys.executable, str(bench_script),
        "--mode", mode,
        "--scale", scale,
        "--concurrency", str(concurrency),
        "--host", host,
    ]
    console.print(f"Running benchmark: mode={mode} scale={scale} concurrency={concurrency}")
    subprocess.run(cmd, check=True)


# ── aeos validate ─────────────────────────────────────────────────────────────

@app.command("validate")
def cmd_validate(
    host: str = typer.Option("http://localhost:8000", "--host", help="AEOS API host"),
):
    """Trigger an on-demand invariant engine evaluation."""
    console.print("Triggering invariant evaluation...")
    result = _api_post("/api/v1/validation/evaluate", {}, host=host)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise typer.Exit(1)

    ok = result.get("ok", False)
    passed = result.get("passed", [])
    violations = result.get("violations", [])

    color = "green" if ok else "red"
    console.print(f"Result: [{color}]{'PASS' if ok else 'FAIL'}[/{color}]  "
                  f"({len(passed)} passed, {len(violations)} violations)")

    for v in violations:
        sev = v.get("severity", "?").upper()
        sev_color = "red" if sev == "CRITICAL" else "yellow"
        console.print(
            f"  [{sev_color}][{sev}][/{sev_color}] {v.get('invariant_id')}: {v.get('message')}"
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app()


if __name__ == "__main__":
    main()
