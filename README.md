# Tuition Fee Agents

A local-first, multi-agent system that reconciles PayPal and bank payments against tuition
enrollments and drafts payment reminders for parents, with a human approving every send.

> Work in progress. See `docs/build-log/` for a step-by-step walkthrough of how it is built.

## Prerequisites

| Tool | Why | Install (macOS) |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python + Python packages | `brew install uv` |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs Postgres locally | `brew install --cask docker`, then open it once |
| make | Project commands | Ships with Xcode Command Line Tools: `xcode-select --install` |

## Quick start

```bash
make setup     # install dependencies, create .env, check prerequisites
make doctor    # re-check anytime
make help      # list all commands
```
