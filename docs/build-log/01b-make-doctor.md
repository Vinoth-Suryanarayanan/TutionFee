# Step 1b — `make doctor`: checking prerequisites

**Goal:** one command that tells a new contributor (or you, on a new laptop) exactly what is
missing and how to fix it — before they hit a confusing error three steps later.

```bash
make doctor     # check only
make setup      # uv sync + create .env, then run doctor
make help       # list all commands
```

Example output while Docker is still installing:

```
✔ uv 0.8.x
✔ Python 3.12.x in .venv, dependencies installed
✔ .env present
✘ Docker is installed but not running
    → Open Docker Desktop and wait for the whale icon to settle
```

## Why not install Docker through uv?

uv installs **Python packages into this project's `.venv`**. Docker Desktop is a **system
application** (it runs a small Linux VM, needs admin rights, is shared by every project). No
Python package manager can install it. The standard pattern is: brew installs system tools, uv
installs Python packages, Docker runs services, and `make` ties them together — with a doctor
command that checks the whole chain.

---

## `Makefile`

`make` is a decades-old build tool that ships with macOS (via Xcode Command Line Tools) and
Linux. We use it as a **menu of project commands**, so nobody has to remember long ones.

```make
.PHONY: help doctor setup
```
Targets are normally file names; `.PHONY` says "these are commands, not files", so `make` runs
them even if a file called `doctor` happens to exist.

```make
help:  ## List available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-8s %s\n", $$1, $$2}'
```
* `help:` defines a target. Being the first target, it's also what plain `make` runs.
* `## List available commands` is just a comment — but `help` finds every line with `## ` and
  prints it, so **the menu documents itself** when we add targets.
* The recipe line **must start with a tab**, not spaces (make's oldest gotcha).
* `@` hides the command itself and shows only its output.
* `$(MAKEFILE_LIST)` = this Makefile's path. `$$1` is `$1` escaped for make (awk's first field).

```make
doctor:  ## Check prerequisites (uv, .venv, .env, Docker)
	@bash scripts/doctor.sh
```
The logic lives in a real script, not inline in the Makefile — easier to read, test, and run
without make.

```make
setup:  ## Install Python dependencies and create .env if missing
	uv sync
	@test -f .env || cp .env.example .env
	@$(MAKE) --no-print-directory doctor
```
* `test -f .env || cp ...` — copy the template only if `.env` doesn't exist, so re-running
  setup never overwrites your real settings.
* `$(MAKE) doctor` — run another target; setup always ends by verifying itself.

## `scripts/doctor.sh`

```bash
#!/usr/bin/env bash
```
"Shebang": run this file with whichever `bash` is first on the PATH.

```bash
set -u
```
Treat use of an undefined variable as an error (catches typos). We deliberately don't use
`set -e` (stop at first failure) because we want to run **every** check and report all problems
at once.

```bash
if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; RESET=""
fi
```
`-t 1` = "is standard output a terminal?". Colors for humans; plain text for CI logs and pipes.
`$'\033[32m'` is the ANSI escape code for green.

```bash
failures=0
ok()   { printf "%s✔%s %s\n" "$GREEN" "$RESET" "$1"; }
fail() { printf "%s✘%s %s\n    → %s\n" "$RED" "$RESET" "$1" "$2"; failures=$((failures + 1)); }
warn() { printf "%s!%s %s\n    → %s\n" "$YELLOW" "$RESET" "$1" "$2"; }
```
Three tiny helpers so every check reads the same: **what's wrong → how to fix it**. `fail`
counts failures; `warn` doesn't.

```bash
if command -v uv >/dev/null 2>&1; then
  ok "$(uv --version)"
else
  fail "uv is not installed" "brew install uv"
fi
```
`command -v uv` succeeds if `uv` is on the PATH. `>/dev/null 2>&1` throws away its output
(both normal and error). `$(...)` inserts a command's output into the message.

```bash
if [ -x .venv/bin/python ]; then
  if .venv/bin/python -c "import pydantic_settings" >/dev/null 2>&1; then
```
Two levels: does the venv exist (`-x` = executable file), and are our dependencies actually
importable inside it? A half-finished `uv sync` would pass the first but fail the second.

```bash
if [ -f .env ]; then ok ".env present"; else fail ".env missing" "cp .env.example .env"; fi
```
`-f` = "regular file exists".

```bash
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
```
Docker has two parts: the `docker` **command** (CLI) and the **engine** (Docker Desktop's VM).
`docker info` only succeeds if the CLI can reach a running engine — exactly the state you're in
right after installing but before opening Docker Desktop.

```bash
    if docker compose version >/dev/null 2>&1; then
```
We start Postgres with `docker compose` (v2, a plugin). Very old installs only have
`docker-compose` with a hyphen.

```bash
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:5432 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "Port 5432 is already in use" "..."
fi
```
`lsof` lists which process holds a network port. If another Postgres (e.g. Postgres.app) is on
5432, our container can't start. A warning, not a failure, because it might be our own database.

```bash
if [ "$failures" -eq 0 ]; then ... exit 0; else ... exit 1; fi
```
The **exit code** is the contract with other tools: `0` = success, anything else = failure.
That's why `make setup` reports an error while Docker isn't running, and why CI can run
`make doctor` as a gate.

## Design decisions

1. **Fail with a fix, not just an error.** Every ✘ line says what to run.
2. **Report everything at once** (no `set -e`), so you fix all issues in one pass.
3. **No dependencies of its own** — plain bash 3.2, because it must work before setup.
