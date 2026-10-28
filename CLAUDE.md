# CLAUDE.md — Claude Code Working Agreement

This repository has **one canonical, tool-neutral engineering policy**: [`AGENTS.md`](AGENTS.md). Claude Code must read it, then [`README.md`](README.md), [`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md), [`docs/AGENT_KICKOFF.md`](docs/AGENT_KICKOFF.md), [`docs/SECURITY.md`](docs/SECURITY.md), and [`docs/ROADMAP.md`](docs/ROADMAP.md). Avoid duplicating or weakening canonical rules here.

## Priority and project scope

- Product owner controls external publishing, pushes, merges, deployment, real accounts, spending, production changes and credentials. Work **locally in Ubuntu / VS Code**; do not change user's global Claude/Codex settings or install new skills/dependencies without permission.
- Python-first. Public research needs no account. Private exchange access (read-only live keys) and testnet-only Trading Mode follow `AGENTS.md` §2 (R8); no live-money orders, withdrawals, transfers or autonomous trading. Correctness and security first, then simplicity, maintainability, performance and cost. Evidence, not a plausible narrative, determines PASS.
- The repository holds the Python engine, tests, Chrome extension and desktop app (it began documentation-only; the V0.1 import was R1). Never claim a feature is tested, or connected to live Binance or an exchange account, unless verified.
- Use six Breakers selectively; reviewers/breakers are **read-only**. Model how market-data timestamps, closed bars, provider failures, duplicate events, user custom prompts and API spending interact. No untrusted prompt execution.
- Avoid inventing code, financial results, risk estimates or tests. Protect truthful git/AI authorship.

## Mandatory security gate

Never commit, print, paste or send `.env`, tokens/API keys, private keys or credential-bearing logs/screenshots. `.gitignore` does **not** protect secrets already tracked or in Git history. Before proposing any commit/push, inspect staged paths and run `python3 tools/secret_guard.py --staged` with sanitized output; follow [`docs/SECURITY.md`](docs/SECURITY.md) and check outgoing history if the user authorizes push. Do not disable hooks, use `--no-verify`, force-add secrets or access live trading credentials. Stop if live keys appear; report safely without values.

## Multi-agent policy: YES, with isolation

Claude Code may use native subagents, teammate features or orchestrated CLI workers **only if the installed version supports them** and task risk/independence justify it. Parallel workers **must not modify the same working tree**. Follow `docs/AGENT_ORCHESTRATION.md`:

1. Lead/coordinator defines contracts: goal, dependencies, exact file ownership, acceptance gates, resource isolation, token/budget cap, and stop condition.
2. Multiple read-only scouts/reviewers/breakers may inspect concurrently; each returns concise reproducible evidence, not product-code edits.
3. Multiple implementers require **separate git worktrees + unique branches**, disjoint owned paths, no writes to shared config/schema/interfaces without a single owner and integration gate. Never silently cherry-pick, merge, force-push, or modify external systems.
4. Lead validates exact diff and tests for each worker, integrates approved local changes in dependency order, and reruns targeted checks; human controls remote operations/merges/releases.
5. After ~3 attempts fail the same gate, stop, challenge assumptions and report a revised plan. Never claim background jobs or subagents ran unless they actually did.

## First-session behavior

Inspect `pwd`, `git status`, branch, files and local tools. Summarize verified repo state (see `docs/PROGRESS.md` and the latest audit in `docs/audits/`), the proposed minimal change and actual tests. Begin with **one implementer**; parallelize only independent **read-only** reviewers until interfaces are stable. Return: `Goal → Changed files → Commands/tests/results → Risks → Next step`.
