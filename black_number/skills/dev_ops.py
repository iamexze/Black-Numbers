"""Developer and operations skills — repositories, code, and machine health.

These exist because the person running this assistant is at a keyboard with
projects open. "What's dirty in that repo", "where is this function", "what's
eating my CPU" are the questions actually asked during a working day, and each
one is faster spoken than typed.

Inspection is READ_ONLY and unattended. Running a project's test suite is not:
it executes code from the repository, which is somebody else's code even when
it is yours, so it is REVERSIBLE and passes the gate with the command shown.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .base import Context, Result, Risk, Skill

HOME = Path.home()

# Directories never walked: they are large, generated, and never the answer.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "vendor", "Pods", ".gradle", ".idea", ".cache",
    "site-packages", ".DS_Store", "coverage", ".terraform",
}
MAX_SEARCH_FILE = 1_500_000
MAX_SEARCH_HITS = 80

STACK_MARKERS = [
    ("pyproject.toml", "Python"), ("requirements.txt", "Python"), ("setup.py", "Python"),
    ("package.json", "Node / JavaScript"), ("deno.json", "Deno"),
    ("Cargo.toml", "Rust"), ("go.mod", "Go"), ("Gemfile", "Ruby"),
    ("pom.xml", "Java / Maven"), ("build.gradle", "Java / Gradle"),
    ("build.gradle.kts", "Kotlin / Gradle"), ("Package.swift", "Swift"),
    ("pubspec.yaml", "Dart / Flutter"), ("composer.json", "PHP"),
    ("Dockerfile", "Docker"), ("CMakeLists.txt", "C / C++"), ("mix.exs", "Elixir"),
]


def _sh(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)


def _resolve(raw: str | None) -> Path:
    p = Path(raw or ".").expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p.resolve()


def _repo_root(path: Path) -> Path | None:
    ok, out = _sh(["git", "rev-parse", "--show-toplevel"], cwd=path if path.is_dir() else path.parent)
    return Path(out) if ok and out else None


# ── git ─────────────────────────────────────────────────────────────────────
def _git_status(a: dict, _c) -> Result:
    path = _resolve(a.get("path"))
    if not path.exists():
        return Result.fail(f"There's nothing at {path}.")
    root = _repo_root(path)
    if root is None:
        return Result.fail(f"{path.name} isn't inside a git repository.")
    ok, branch = _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    _, porcelain = _sh(["git", "status", "--porcelain"], cwd=root)
    _, upstream = _sh(["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=root)

    lines = [ln for ln in porcelain.splitlines() if ln.strip()]
    staged = sum(1 for ln in lines if ln[:1] not in (" ", "?"))
    unstaged = sum(1 for ln in lines if ln[1:2] not in (" ",) and ln[:1] != "?")
    untracked = sum(1 for ln in lines if ln.startswith("??"))
    ahead = behind = 0
    if re.fullmatch(r"\d+\s+\d+", upstream.strip()):
        behind, ahead = (int(x) for x in upstream.split())

    bits = []
    if staged:
        bits.append(f"{staged} staged")
    if unstaged:
        bits.append(f"{unstaged} modified")
    if untracked:
        bits.append(f"{untracked} untracked")
    if ahead:
        bits.append(f"{ahead} ahead")
    if behind:
        bits.append(f"{behind} behind")
    state = ", ".join(bits) if bits else "clean"
    return Result.say(
        f"{root.name} is on {branch}, {state}.",
        detail=f"{root}\nbranch: {branch}\n" + ("\n".join(lines[:40]) or "working tree clean"),
        data={"repo": str(root), "branch": branch, "staged": staged, "modified": unstaged,
              "untracked": untracked, "ahead": ahead, "behind": behind, "clean": not lines},
    )


def _git_log(a: dict, _c) -> Result:
    path = _resolve(a.get("path"))
    root = _repo_root(path)
    if root is None:
        return Result.fail("That isn't a git repository.")
    count = max(1, min(int(a.get("count") or 8), 50))
    ok, out = _sh(["git", "log", f"-{count}", "--date=short",
                   "--pretty=format:%h  %ad  %an  %s"], cwd=root)
    if not ok:
        return Result.fail("Couldn't read the history.", detail=out)
    first = out.splitlines()[0] if out else ""
    subject = first.split("  ", 3)[-1] if first else "nothing yet"
    return Result.say(f"Latest in {root.name}: {subject}", detail=out)


# ── code ────────────────────────────────────────────────────────────────────
def _walk(root: Path, glob: str = "*"):
    """Yield candidate files, pruning generated and vendored trees as we go."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if glob != "*" and not p.match(glob):
                continue
            yield p


def _is_text(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\0" not in f.read(2048)
    except Exception:
        return False


def _code_search(a: dict, _c) -> Result:
    pattern = a.get("pattern") or ""
    if not pattern:
        return Result.fail("Search for what?")
    root = _resolve(a.get("path"))
    if not root.is_dir():
        return Result.fail(f"{root} isn't a folder.")
    try:
        rx = re.compile(pattern if a.get("regex") else re.escape(pattern), re.I)
    except re.error as e:
        return Result.fail(f"That isn't a valid pattern: {e}")
    glob = a.get("glob") or "*"

    hits: list[str] = []
    scanned = 0
    for p in _walk(root, glob):
        if len(hits) >= MAX_SEARCH_HITS:
            break
        try:
            if p.stat().st_size > MAX_SEARCH_FILE or not _is_text(p):
                continue
            scanned += 1
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    rel = p.relative_to(root)
                    hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        break
        except (OSError, PermissionError):
            continue
    if not hits:
        return Result.say(f"No matches for {pattern} in {root.name} ({scanned} files read).")
    files = len({h.split(":")[0] for h in hits})
    return Result.say(
        f"{len(hits)} {'match' if len(hits) == 1 else 'matches'} across {files} "
        f"{'file' if files == 1 else 'files'}.",
        detail="\n".join(hits), data=hits,
    )


def _project_scan(a: dict, _c) -> Result:
    root = _resolve(a.get("path"))
    if not root.is_dir():
        return Result.fail(f"{root} isn't a folder.")
    stacks = sorted({label for marker, label in STACK_MARKERS if (root / marker).exists()})

    ext: dict[str, int] = {}
    total = 0
    for p in _walk(root):
        total += 1
        if total > 20000:
            break
        ext[p.suffix.lower() or "(none)"] = ext.get(p.suffix.lower() or "(none)", 0) + 1
    top = sorted(ext.items(), key=lambda kv: -kv[1])[:8]

    rows = [("path", str(root)), ("stack", ", ".join(stacks) or "not recognised"),
            ("files", f"{total}{'+' if total > 20000 else ''}")]
    root_git = _repo_root(root)
    if root_git:
        ok, branch = _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root_git)
        rows.append(("git", f"{root_git.name} on {branch}"))
    for name in ("README.md", "readme.md", "Makefile", "docker-compose.yml", ".env.example"):
        if (root / name).exists():
            rows.append(("has", name))
    tests = [d for d in ("tests", "test", "spec", "__tests__") if (root / d).is_dir()]
    if tests:
        rows.append(("tests", ", ".join(tests)))
    if (root / "package.json").exists():
        try:
            import json
            pkg = json.loads((root / "package.json").read_text())
            if pkg.get("scripts"):
                rows.append(("npm scripts", ", ".join(list(pkg["scripts"])[:8])))
        except Exception:
            pass
    rows.append(("file types", ", ".join(f"{k} {v}" for k, v in top)))
    return Result.say(
        f"{root.name}: {', '.join(stacks) or 'unrecognised stack'}, {total} files.",
        detail="\n".join(f"{k:12} {v}" for k, v in rows),
        data={"path": str(root), "stacks": stacks, "files": total},
    )


def _test_command(root: Path) -> list[str] | None:
    if (root / "package.json").exists():
        try:
            import json
            if "test" in (json.loads((root / "package.json").read_text()).get("scripts") or {}):
                return ["npm", "test", "--silent"]
        except Exception:
            pass
    if (root / "Cargo.toml").exists():
        return ["cargo", "test"]
    if (root / "go.mod").exists():
        return ["go", "test", "./..."]
    for direct in ("tests/test_core.py", "tests/run.py"):
        if (root / direct).exists():
            return ["python3", direct]
    if any((root / m).exists() for m in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")):
        return ["python3", "-m", "pytest", "-q"]
    if (root / "Makefile").exists():
        try:
            if re.search(r"^test:", (root / "Makefile").read_text(), re.M):
                return ["make", "test"]
        except Exception:
            pass
    return None


def _run_tests(a: dict, ctx: Context) -> Result:
    root = _resolve(a.get("path"))
    if not root.is_dir():
        return Result.fail(f"{root} isn't a folder.")
    cmd = _test_command(root)
    if cmd is None:
        return Result.fail(f"I can't tell how {root.name} runs its tests.")
    if not shutil.which(cmd[0]):
        return Result.fail(f"{cmd[0]} isn't installed, so I can't run the suite.")
    ctx.speak(f"Running {' '.join(cmd)}.")
    started = time.perf_counter()
    ok, out = _sh(cmd, cwd=root, timeout=int(a.get("timeout") or 300))
    took = time.perf_counter() - started
    tail = "\n".join(out.splitlines()[-40:])
    summary = next((ln for ln in reversed(out.splitlines())
                    if re.search(r"\b(passed|failed|ok|FAIL|error)\b", ln, re.I)), "")
    speech = (f"Tests passed in {took:.1f} seconds." if ok
              else f"Tests failed after {took:.1f} seconds.")
    if summary:
        speech += f" {summary.strip()[:120]}"
    return Result(ok, speech, detail=f"$ {' '.join(cmd)}\n{tail}",
                  data={"ok": ok, "seconds": round(took, 2), "command": cmd})


# ── machine health ──────────────────────────────────────────────────────────
def _machine_health(_a, _c) -> Result:
    rows: list[tuple[str, str]] = []
    ok, up = _sh(["uptime"], timeout=6)
    load = ""
    if ok:
        m = re.search(r"load averages?:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", up)
        if m:
            load = m.group(1)
            rows.append(("load", f"{m.group(1)} / {m.group(2)} / {m.group(3)}  (1m/5m/15m)"))
        m2 = re.search(r"up\s+(.+?),\s+\d+\s+users?", up)
        if m2:
            rows.append(("uptime", m2.group(1)))

    cores = 0
    ok, out = _sh(["sysctl", "-n", "hw.ncpu"], timeout=5)
    if ok and out.strip().isdigit():
        cores = int(out.strip())
        rows.append(("cores", str(cores)))

    # Memory: total from sysctl, used from vm_stat page counts. Anything active,
    # wired or compressed is genuinely occupied; free and inactive are not.
    used_pct = None
    ok, total_raw = _sh(["sysctl", "-n", "hw.memsize"], timeout=5)
    ok2, vm = _sh(["vm_stat"], timeout=6)
    if ok and ok2 and total_raw.strip().isdigit():
        total = int(total_raw.strip())
        page = 4096
        pm = re.search(r"page size of (\d+) bytes", vm)
        if pm:
            page = int(pm.group(1))
        def pages(label: str) -> int:
            m = re.search(rf"{label}:\s+(\d+)\.", vm)
            return int(m.group(1)) if m else 0
        used = (pages("Pages active") + pages("Pages wired down")
                + pages("Pages occupied by compressor")) * page
        used_pct = used / total * 100
        rows.append(("memory", f"{used/1e9:.1f} GB of {total/1e9:.1f} GB used ({used_pct:.0f}%)"))

    ok, df = _sh(["df", "-h", "/"], timeout=6)
    if ok and len(df.splitlines()) > 1:
        f = df.splitlines()[1].split()
        if len(f) >= 5:
            rows.append(("disk /", f"{f[2]} used of {f[1]}, {f[3]} free ({f[4]})"))

    ok, ps = _sh(["ps", "-Ao", "pcpu,comm", "-r"], timeout=8)
    hogs = []
    if ok:
        for line in ps.splitlines()[1:6]:
            parts = line.split(None, 1)
            if len(parts) == 2:
                hogs.append(f"{parts[0]:>6}%  {Path(parts[1].strip()).name[:40]}")
    if hogs:
        rows.append(("top CPU", hogs[0].strip()))

    # A spoken summary states the one thing that is wrong, or that nothing is.
    concerns = []
    if load and cores and float(load) > cores * 1.5:
        concerns.append(f"load {load} against {cores} cores")
    if used_pct is not None and used_pct > 90:
        concerns.append(f"memory {used_pct:.0f}% used")
    speech = ("The machine looks healthy." if not concerns
              else "Worth a look: " + ", and ".join(concerns) + ".")
    detail = "\n".join(f"{k:10} {v}" for k, v in rows)
    if hogs:
        detail += "\n\ntop processes by CPU:\n" + "\n".join("  " + h for h in hogs)
    return Result.say(speech, detail=detail, data=dict(rows))


def _disk_usage(a: dict, _c) -> Result:
    path = _resolve(a.get("path"))
    if not path.exists():
        return Result.fail(f"There's nothing at {path}.")
    ok, out = _sh(["du", "-sh", str(path)], timeout=60)
    if not ok:
        return Result.fail("Couldn't measure that.", detail=out)
    size = out.split()[0] if out.split() else "?"
    return Result.say(f"{path.name or path} is {size}.", detail=out, data={"size": size})


def _open_in_editor(a: dict, _c) -> Result:
    path = _resolve(a.get("path"))
    if not path.exists():
        return Result.fail(f"There's nothing at {path}.")
    editor = a.get("editor") or os.environ.get("BN_EDITOR") or ""
    for candidate in ([editor] if editor else []) + ["code", "cursor", "subl", "zed"]:
        if candidate and shutil.which(candidate):
            ok, out = _sh([candidate, str(path)], timeout=15)
            if ok:
                return Result.say(f"Opened {path.name} in {candidate}.")
    ok, out = _sh(["open", str(path)], timeout=10)
    return (Result.say(f"Opened {path.name}.") if ok
            else Result.fail(f"Couldn't open {path.name}.", detail=out))


def skills() -> list[Skill]:
    return [
        Skill(
            "git_status", "Report a repository's branch and what is uncommitted.",
            _git_status, parameters={"path": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "git_log", "Read a repository's recent commits.", _git_log,
            parameters={"path": {"type": "string"}, "count": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "code_search", "Search a folder's text files for a string or regex.",
            _code_search,
            parameters={
                "pattern": {"type": "string", "required": True},
                "path": {"type": "string"},
                "glob": {"type": "string", "description": "e.g. '*.py'"},
                "regex": {"type": "boolean", "description": "treat pattern as a regex"},
            },
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "project_scan", "Identify a project's stack, size and entry points.",
            _project_scan, parameters={"path": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "run_tests", "Detect and run a project's test suite.", _run_tests,
            parameters={"path": {"type": "string"}, "timeout": {"type": "integer"}},
            risk=Risk.REVERSIBLE,
            caution="this executes the project's own code",
        ),
        Skill("machine_health", "Report load, memory, disk and the busiest processes.",
              _machine_health, risk=Risk.READ_ONLY),
        Skill(
            "disk_usage", "Measure how much space a folder uses.", _disk_usage,
            parameters={"path": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "open_in_editor", "Open a file or folder in a code editor.", _open_in_editor,
            parameters={"path": {"type": "string", "required": True},
                        "editor": {"type": "string"}},
            risk=Risk.REVERSIBLE,
        ),
    ]
