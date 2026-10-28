# Evidence-Gated Multi-Agent Orchestration — My AI Trading

**Security requirement:** every worktree must follow [`SECURITY.md`](SECURITY.md). Never stage `.env`/secrets or bypass pre-commit/pre-push gates; before integrating, validate each worktree with `python3 tools/secret_guard.py --staged`.

**Status:** operating policy for local Codex / Claude Code in VS Code; does **not** claim that multi-agent tools, agent skills or worktrees are installed/enabled. **Canonical rules:** [`../AGENTS.md`](../AGENTS.md). Grounded in Engineering AI Harness Doctrine (2026-10-17), the six Breakers, EGAO, concurrency safety, REA, minimalism, token budgeting and explicit human approval gates. Apply only relevant skills per task, not all skills on every change.

## 1. Design principles

- **Parallel only when independent.** More agents does not imply more progress, quality or cheaper tokens. Start with one lead and one implementer; add specialist review or independent implementation only when measured coordination cost is justified.
- **One source of truth / single writer.** Never allow two coding workers to write the same file or mutate shared data at the same time. Prefer isolated worktrees and read-only contract snapshots to locking a shared workspace. Shared API contracts, rule DSL schema, data models, central `pyproject.toml`, CI and migrations have exactly one responsible writer per integration checkpoint.
- **Read-only QA independence.** Six breaker personas (Security, Data, Flow, Environment, Performance, Efficiency & Simplicity) and other reviewers report evidence only, not fixes. Primary implementer fixes issues; independent retest verifies.
- **Bounded and reversible.** Use explicit budget, ≤ ~3 loops for repeated failures, no silent refactoring, no hidden destructive Git commands, no global setting changes. Local diff/branch/worktree creation within agreed scope only; remote push, merge, deploy and paid external tests require owner approval.
- **Isolated external effects.** Mock market feeds, use temporary fixtures and separate temp directories / test databases for concurrent workers. Never share exchange secrets, use production accounts or call order/private endpoints. Limit any paid LLM call by an approved budget and sanitize provider-bound payloads.

## 2. Worker classes / permissions

| Role | Writes application files? | May run offline checks? | Integration and external actions |
|---|---|---|---|
| **Lead / Orchestrator** | In dedicated integration worktree only, within agreed scope | Yes | Integrates locally and reports evidence; **does not push/merge/deploy** |
| **Implementer** | Yes, only in **owned files** in its own worktree | Yes | Returns diff, commit or patch and test output; no remote actions |
| **Scout / Researcher** | **No** | Read-only commands | Returns facts, source links, unknowns |
| **Breaker / Independent Reviewer / Judge** | **No** | Tests in own isolated scratch context, no source modifications | Returns PASS/FAIL/INCONCLUSIVE plus defects; no fixes |

A local read-only review may run in the same repository snapshot only if commands do not create output in the source tree (e.g., use external tmp directories). Never let a reviewer auto-format, install dependencies or rewrite lockfiles.

## 3. Task contract before delegation

```yaml
task_id: R2-market-data-normalization
role: implementer
owner: worker-data
base_sha: "<exact SHA>"
depends_on: []
owned_files:
  - src/my_ai_trading/market_data/**
  - tests/market_data/**
read_only_files:
  - src/my_ai_trading/domain/models.py
shared_resources:
  - "shared type contract: immutable until integration"
acceptance_tests:
  - "pytest -q tests/market_data"
  - "closed-bar / out-of-order / duplicate fixture assertions"
evidence_required:
  - "diff, commands, exit codes, regression test names"
budget: "no paid APIs; bounded tokens and 2 offline test rounds initially"
stop_conditions:
  - "blocked by schema/API change"
  - "3 failed attempts at same gate"
handoff: "diff + checks + conflicts + risks, no auto-merge"
```

The coordinator explicitly states file conflicts and dependencies; **no handoff without an exact base commit**. A task cannot be marked PASS if blocked tests were skipped or a source path changed without permission.

## 4. Practical Ubuntu worktree example (OPTIONAL; do not run blindly)

Run from cloned project after first checking `git status --short`, `git branch --show-current`, and current HEAD. Paths below are examples: change them if actual packages/modules differ. Avoid commands on an uncommitted or dirty base unless the user approves isolation/stashing plan.

```bash
# Assuming a clean checked-out base branch 'main' and a repository at ~/Projects/my-ai-trading
git status --short
git rev-parse HEAD

git worktree add ../my-ai-trading-data -b feat/r2-market-data main
git worktree add ../my-ai-trading-strategies -b feat/r2-strategy-rules main

git worktree list
# Open independent VS Code windows only when the task contracts are independent:
code ../my-ai-trading-data
code ../my-ai-trading-strategies
```

- Worker A owns `market_data/**` and corresponding tests, reads but does not modify domain types.
- Worker B owns `strategy/**` and corresponding tests, reads immutable data contracts.
- Lead owns shared `domain/models.py`, central config, integration and conflict resolution.
- **Do not run these workers together** if the strategy implementation depends on an unfinalized market-data model. Sequence the dependency (models → market data → strategy) or agree on a versioned testable contract first.
- Git worktrees isolate working files, but **share the repository object store and some Git refs**: no concurrent branch/ref mutations, deletion, `reset --hard`, `clean -fdx`, worktree prune, cherry-pick or rebase initiated by independent workers. The lead sequences integration.
- Concurrent tests may still share ports, cache directories, SQLite files or paid accounts: give each worker independent `TMPDIR`, caches, ports and fixtures and do not write shared DB tables.

## 5. Integration gates

1. **Before work:** read relevant skills/doctrine portions; check status/base SHA, dependency DAG, file ownership and current repo evidence; agree on per-task token/compute cap.
2. **Each worker:** produce a small diff, local focused tests and structured handoff. No claiming PASS if tests were not run. Missing data → INCONCLUSIVE.
3. **Coordinator:** inspect `git diff` and owned paths; ensure API/DSL/data contracts were respected; stage integrations one worker at a time in a dedicated worktree after authorized local edits; run targeted tests after each integration.
4. **Independent gate:** run risk-based breakers. P0/P1 block completion; any accepted unresolved issues must have owner, reason and explicit scope. For releases, run all relevant six breakers + regressions + performance baseline and recheck exact integrated HEAD.
5. **User gate:** user reviews actual diff, authorship, security, tests and decides whether to commit/push/open PR/merge/deploy. Bot auto-merge is not authorized by this document.

## 6. Minimum handoff output

```yaml
task_id: R2-market-data-normalization
head_sha: "<full SHA or unavailable>"
role: implementer
scope: ["market_data", "tests/market_data"]
verdict: PASS # or FAIL / INCONCLUSIVE
changed_files: []
checks_run:
  - "pytest -q tests/market_data (exit=0, 12 passed)" # example only, NEVER invent results
findings:
  - "none observed" # only if checked, not proof of absence
untested: ["live Binance API (not approved)"]
resource_spend: "0 external API calls"
risks: []
next_action: "lead to review exact diff before integration"
```

## 7. Suggested collaboration for this project

- **R1 (done):** 1 implementer audited/imported V0.1; Security + Data reviewers may independently inspect ZIP/read-only, with separate evidence. Do not run strategy feature writers yet.
- **R2:** if stable schemas are fixed, data ingestion and CLI/UX can be implemented in separate worktrees with exclusive files; Flow/Performance breakers inspect concurrently.
- **R3/R4:** strategy compiler + AI router can be split only after contract types are frozen. Test prompt injection, untrusted model output, strict cost caps and cross-tenant cache separation before integration.
- **R5+:** UI, market engine, data persistence and AI providers may be separate workstreams once independent contracts and CI gates exist.

Do not use all 20 preset personas as coding agents, nor one LLM call per persona per bar. Parallel agent work is an engineering optimization and must be measured, not assumed.
