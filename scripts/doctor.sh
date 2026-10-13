#!/usr/bin/env bash
# Checks that everything this project needs is installed and running.
# Plain bash (works with the bash 3.2 that ships with macOS); needs nothing from .venv,
# because its whole job is to run *before* things are set up.

set -u

# Colors only when printing to a terminal (keeps CI logs and pipes clean).
if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; RESET=""
fi

failures=0

ok()   { printf "%s✔%s %s\n" "$GREEN" "$RESET" "$1"; }
fail() { printf "%s✘%s %s\n    → %s\n" "$RED" "$RESET" "$1" "$2"; failures=$((failures + 1)); }
warn() { printf "%s!%s %s\n    → %s\n" "$YELLOW" "$RESET" "$1" "$2"; }

echo "Checking prerequisites..."
echo

# 1. uv — installs Python and our Python packages.
if command -v uv >/dev/null 2>&1; then
  ok "$(uv --version)"
else
  fail "uv is not installed" "brew install uv"
fi

# 2. Project virtual environment with dependencies installed.
if [ -x .venv/bin/python ]; then
  if .venv/bin/python -c "import pydantic_settings" >/dev/null 2>&1; then
    ok "Python $(.venv/bin/python -c 'import platform; print(platform.python_version())') in .venv, dependencies installed"
  else
    fail ".venv exists but dependencies are missing" "uv sync"
  fi
else
  fail "No .venv yet" "uv sync"
fi

# 3. Local settings file.
if [ -f .env ]; then
  ok ".env present"
else
  fail ".env missing" "cp .env.example .env"
fi

# 4. Docker — CLI installed, engine running, compose plugin available.
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) running"
    if docker compose version >/dev/null 2>&1; then
      ok "$(docker compose version)"
    else
      fail "docker compose plugin not found" "Update Docker Desktop"
    fi
  else
    fail "Docker is installed but not running" "Open Docker Desktop and wait for the whale icon to settle"
  fi
else
  fail "Docker is not installed" "brew install --cask docker, then open Docker Desktop"
fi

# 5. Port 5432 — Postgres will listen here. Warn only: it may be our own container.
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:5432 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "Port 5432 is already in use" "Fine if it's this project's database; otherwise stop the other Postgres"
fi

echo
if [ "$failures" -eq 0 ]; then
  printf "%sAll good.%s\n" "$GREEN" "$RESET"
  exit 0
else
  printf "%s%d problem(s) found.%s Fix the items marked ✘ and run 'make doctor' again.\n" "$RED" "$failures" "$RESET"
  exit 1
fi
