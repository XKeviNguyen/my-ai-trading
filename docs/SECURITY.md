# 🔐 Security-first development — local Codex / Claude Code

**Status:** documented safeguards and optional hooks, not proof of security or a claim that your local Ubuntu has them enabled. No repository push, merge, or deployment is authorized by these instructions.

## 1. Secrets: never commit or send to an AI agent

- Never commit `.env`, `.env.local`, `.env.production`, real tokens/API keys, SSH/private keys, browser cookies, `.aws/credentials`, provider service-account files, or screenshots/logs with credentials.
- `.env.example` / `.env.sample` may contain **variable names and EMPTY or obviously fake values only**. Never copy actual credentials into examples or tests.
- `.gitignore` protects only **untracked files matched by its rules**. Already-tracked files, `git add -f`, secrets in ordinary source/Markdown, and previously committed history are not secured by `.gitignore`.
- Do not feed trade-account identifiers, portfolio data, private strategies or exchange credentials into third-party LLM/Vision providers without explicit opt-in; never send screenshots containing secrets.
- Public analysis uses **public market-data endpoints only** and needs no keys. Exchange accounts (R8, `AGENTS.md` §2) are opt-in: live keys must be read-only (keys with trade, withdrawal or transfer rights are rejected at connection time), and orders go only to official testnet/demo environments. Exchange credentials are stored only in process memory or the OS credential vault with a user-chosen expiry, never in Git, SQLite, `.env`, browser storage, frontend bundles, logs, crash reports or LLM prompts, and API responses never return them.

## 2. Local secret gate: enable once after reviewing hooks

The kit includes a **dependency-free Python `tools/secret_guard.py`** and optional Git `pre-commit` and `pre-push` hooks. They reject risky credential file names, common private-key/token patterns, and suspicious long hard-coded credentials. They **do not detect every secret** and are not a substitute for a maintained scanner such as Gitleaks plus review. Hook configuration changes only this repository and are **not performed automatically**.

```bash
# From the cloned repository root, after reviewing .githooks/* and tools/secret_guard.py
python3 tools/secret_guard.py --staged
# Explicit opt-in, only if you approve it:
git config --local core.hooksPath .githooks
# Check the repo-local config:
git config --local --get core.hooksPath
```

Pre-commit checks content in the **Git index** (what will be committed), not merely your working directory. Pre-push checks the contents of **outgoing commits, including historical versions**; adding a secret and deleting it in the next commit is *still a leak*. For a new remote branch, it scans reachable history, subject to conservative size limits; commits beyond those limits are blocked pending a full external scan. Binary files are not content-scanned; review them separately. Hooks can be bypassed with `--no-verify`, or not installed, so the human must review the diff and enable hosting-side secret scanning/push protection where available.

**Full-history scan in CI.** The `secret-scan` job in `.github/workflows/ci.yml` checks out the full history (`fetch-depth: 0`) and runs `python3 tools/secret_guard.py --history`. It checks every file revision in every commit reachable from the tested commit, including merge commits, by file name and by content. It refuses a shallow clone, never prints secret values, and has no size limit on the number of commits. You can run the same command locally. It uses the same conservative patterns as the hooks, so it is not a full replacement for a maintained scanner.

A dedicated full-history scanner (Gitleaks or equivalent, installed only with your approval) is still recommended, with a pinned trusted version, reviewed configuration and sanitized logs. Adding it is an **owner decision**. Treat third-party scanners as optional dependencies, not as an excuse to skip safety review.

## 3. Safe staging and proof before any commit/push

```bash
git status --short
git diff --cached --name-only
git diff --cached --check
python3 tools/secret_guard.py --staged
git ls-files | grep -E '(^|/)\.env(\.|$)' || true
# Review only file paths; NEVER cat/print secrets to chat or CI.
```

If `.env` appears in `git ls-files`, it has been tracked and must be removed from the index **without deleting your local file**:

```bash
git rm --cached -- .env
# Then review status, staged changes and run secret checks.
```

For other paths use `git rm --cached -- <path>` (after confirming the target). If a secret was ever committed or pushed, **immediately revoke/rotate it**; deleting the file in a new commit does not erase it from Git history. History rewriting requires a reviewed plan and explicit user authorization; coordinate if the remote already has the secret. Never paste the key into a chat, issue, log or bug report.

## 4. Remote, CI and AI agents

- GitHub visibility: **Private initially** was the plan; the repository is now **public**. The 2026-10-27 audit found `main` **unprotected** (no branch protection or rulesets). Owner action: require pull requests and the passing `tests` checks on `main`, block force-pushes, and enable GitHub secret scanning and push protection. Remote controls are another layer, not automatic assumptions about account settings.
- CI (`.github/workflows/ci.yml`) has read-only `contents` permission, checks out without persisted credentials, and pins every action to a full commit SHA of a released tag (the tag is in a comment). Update a pin only to a SHA you verified against the action's official repository.
- Codex/Claude agents may scan, review, and prepare local diffs; **no push, merge, deploy, repository publishing, security-bypass flags, or Git-history rewrite** without owner approval. Do not set `HUSKY=0`, use `--no-verify`, disable security workflows, or edit hook checks to force a passing result.
- User-authored/custom strategy prompts are untrusted input. Parse into a bounded typed rule AST; no arbitrary Python, `eval`, shell, SQL or unrestricted tool access. Block prompt injection from charts, screenshots, news feeds, and model output.
- Security reviews and all six Breakers are evidence-driven; independent reviewers are read-only. Priority: correctness/security → clarity/simplicity/maintainability → performance/cost.
- If a production key or live credentials are discovered, **stop, avoid echoing the value**, notify the owner using only sanitized metadata and request explicit direction. If exposure is confirmed, rotate/revoke using the provider workflow under owner control.

## 5. Test boundaries for a trading application

- Use mocked feeds, offline fixtures, official testnet/demo accounts, and bounded spend. No real-money orders, withdrawals, unsupported scraping or automated trading. Live accounts are read-only and are never used by automated tests.
- Closed-candle and UTC rules, time series integrity, deduplicated alerts, rate limiting/backoff, provider timeouts, LLM per-user spend caps and tenant isolation should be independently tested before release.
- Sensitive datasets, model checkpoints, screenshots and market provider licensing require explicit scope and storage handling. Never assume a Python test suite or local hook proves the system is completely secure.

## 6. Dependencies and supply chain (current state)

| Area | Pinning today | Gap |
|---|---|---|
| Python (`pyproject.toml`, `desktop/pyproject.toml`) | Version **ranges** only (for example `fastapi>=0.120,<1`). No lockfile, no constraints file, no hashes. | CI and each install can resolve different versions. A compromised or broken release inside a range would be picked up. The provenance of `httpx2` (used by the Starlette test client) has not been verified. |
| Chrome extension (`extension/package-lock.json`) | Lockfile v3 with an exact version and an `integrity` (SHA-512) hash for the only dependency, the dev dependency `typescript`. CI installs with `npm ci --ignore-scripts`. | None known. Runtime code has no npm dependencies. |
| GitHub Actions | Every action pinned to a full commit SHA. | Pins must be updated by hand. |
| System packages (CI desktop job) | `apt-get` packages from the runner's Ubuntu archive, not pinned. | Accepted for CI-only libraries. |

No dependency audit tool (`pip-audit`, `npm audit` in CI, OSV) runs today. Recommended follow-up, as an **owner decision** because it adds tooling: commit a constraints or lock file with hashes for the CI and development installs (for example generated with `pip-compile --generate-hashes` or `uv lock`), install with `--require-hashes`, and add a pinned vulnerability audit job. Until then, review dependency updates by hand.
