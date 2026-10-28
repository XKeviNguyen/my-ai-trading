#!/usr/bin/env python3
"""Conservative, dependency-free staged/history secret gate for local development and CI.

Checks staged additions, outgoing Git commits, or (with --history) every file
revision reachable from HEAD. It is defense in depth, not a replacement for
dedicated secret scanners, human review, or GitHub protections.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_COMMITS = 1000
MAX_CHANGED_FILES = 10000
NULL_SHA = re.compile(r"^0+$")
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf", ".zip", ".gz", ".mp4", ".mov", ".woff", ".woff2"}
SECRET_VALUE_PATTERN = re.compile(
    rb"(?i)(?<![A-Za-z0-9])(?:sk-proj-|sk-svcacct-|sk-ant-|gh[pousr]_|github_pat_|sk_live_)[A-Za-z0-9_-]{12,}"
)
PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
AWS_KEY_PATTERN = re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
ASSIGNMENT_PATTERN = re.compile(
    rb"(?im)^\s*(?:export\s+)?(?:[A-Za-z0-9_]*?(?:API_KEY|API_SECRET|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|SECRET_KEY|PASSWORD|PASSPHRASE|PRIVATE_KEY))\s*[:=]\s*['\"]?([^\s\"',;#]+)",
)
PLACEHOLDERS = (b"example", b"sample", b"your", b"dummy", b"placeholder", b"replace", b"test", b"fake", b"changeme", b"xxxx", b"os.getenv", b"process.env", b"${", b"<", b"...", b"none", b"null")


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    run = subprocess.run(["git", *args], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if run.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed (code {run.returncode}); refusing to proceed")
    return run.stdout


def unsafe_filename(name: str) -> bool:
    path = PurePosixPath(name)
    base = path.name.lower()
    if (base == ".env" or base.startswith(".env.")) and base not in {".env.example", ".env.sample"}:
        return True
    if base in {".envrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json", "service-account.json", "service_account.json", "known_hosts.private"}:
        return True
    if base.startswith(("service-account-", "service_account_")) and base.endswith(".json"):
        return True
    if path.suffix.lower() in {".pem", ".p12", ".pfx", ".key", ".keystore", ".jks"}:
        return True
    parts = [p.lower() for p in path.parts]
    if ".aws" in parts and base == "credentials":
        return True
    return False


def scan_bytes(data: bytes) -> str | None:
    if PRIVATE_KEY_PATTERN.search(data):
        return "private key block"
    if AWS_KEY_PATTERN.search(data):
        return "AWS access key pattern"
    if SECRET_VALUE_PATTERN.search(data):
        return "provider token pattern"
    for match in ASSIGNMENT_PATTERN.finditer(data):
        value = match.group(1).strip().lower()
        if len(value) >= 12 and not any(value.startswith(p) for p in PLACEHOLDERS):
            return "possible hard-coded credential assignment"
    return None


def scan_blob(path: str, spec: str) -> str | None:
    if unsafe_filename(path):
        return "sensitive filename"
    size = int(git("cat-file", "-s", spec).decode().strip())
    if size > MAX_BLOB_BYTES:
        if PurePosixPath(path).suffix.lower() in BINARY_EXTENSIONS:
            return None  # Binary contents require separate manual review.
        return "text/blob exceeds scan limit; review and scan separately"
    if PurePosixPath(path).suffix.lower() in BINARY_EXTENSIONS:
        return None
    content = git("show", spec)
    if b"\x00" in content:
        return None
    return scan_bytes(content)


def staged() -> list[tuple[str, str]]:
    paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z").split(b"\0")
    return [(p.decode("utf-8", "surrogateescape"), ":" + p.decode("utf-8", "surrogateescape")) for p in paths if p]


def outgoing() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    commits_scanned: set[str] = set()
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError("invalid Git pre-push ref description")
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if NULL_SHA.fullmatch(local_sha):
            continue  # Branch deletion: no new content to push.
        if NULL_SHA.fullmatch(remote_sha):
            history = git("rev-list", local_sha)
        else:
            git("cat-file", "-e", remote_sha + "^{commit}")
            history = git("rev-list", f"{remote_sha}..{local_sha}")
        for c in history.decode().splitlines():
            if c in commits_scanned:
                continue
            commits_scanned.add(c)
            if len(commits_scanned) > MAX_COMMITS:
                raise RuntimeError("too many outgoing commits; use dedicated history scan and review")
            files = git("diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "--diff-filter=ACMRT", "-z", c).split(b"\0")
            for path_bytes in files:
                if not path_bytes:
                    continue
                path = path_bytes.decode("utf-8", "surrogateescape")
                item = (path, f"{c}:{path}")
                if item not in seen:
                    pairs.append(item)
                    seen.add(item)
                if len(pairs) > MAX_CHANGED_FILES:
                    raise RuntimeError("too many outgoing file revisions; dedicated history scan required")
    return pairs


def history() -> list[tuple[str, str]]:
    """Every file revision added or modified in any commit reachable from HEAD.

    Each path is checked by name and each blob by content. Merge commits are diffed
    against every parent (-m), so content that first appears in a merge is covered.
    Needs the full history: in CI, check out with fetch-depth: 0.
    """
    if git("rev-parse", "--is-shallow-repository").strip() == b"true":
        raise RuntimeError("shallow clone; full history is required (fetch-depth: 0)")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for c in git("rev-list", "HEAD").decode().splitlines():
        raw = git("diff-tree", "--root", "-m", "-r", "--no-commit-id", "--diff-filter=ACMRT", "-z", c)
        fields = raw.split(b"\0")
        i = 0
        while i < len(fields):
            meta = fields[i]
            i += 1
            if not meta.startswith(b":"):
                continue
            parts = meta[1:].split()
            if len(parts) != 5 or i >= len(fields):
                raise RuntimeError("unexpected git diff-tree output; refusing to proceed")
            new_mode, blob, status = parts[1], parts[3].decode(), parts[4][:1]
            path_bytes = fields[i]
            i += 1
            if status in (b"R", b"C"):
                path_bytes = fields[i]  # Renames and copies carry a second (destination) path.
                i += 1
            if new_mode == b"160000":
                continue  # Submodule commit pointer, not a blob.
            item = (path_bytes.decode("utf-8", "surrogateescape"), blob)
            if item not in seen:
                seen.add(item)
                pairs.append(item)
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="Refuse commits/pushes with likely secrets")
    parser.add_argument("--staged", action="store_true", help="scan the staged index")
    parser.add_argument("--pre-push", action="store_true", help="scan outgoing commits from Git pre-push stdin")
    parser.add_argument("--history", action="store_true", help="scan every file revision reachable from HEAD (CI)")
    args = parser.parse_args()
    if [args.staged, args.pre_push, args.history].count(True) != 1:
        parser.error("choose exactly one of --staged, --pre-push or --history")
    try:
        if args.history:
            pairs = history()
        else:
            pairs = staged() if args.staged else outgoing()
        issues = []
        for path, spec in pairs:
            problem = scan_blob(path, spec)
            if problem:
                issues.append((path, problem))
        if issues:
            print(f"SECRET GUARD: BLOCKED ({len(issues)} potential issue(s))", file=sys.stderr)
            for path, why in issues[:30]:
                print(f"  {path}: {why}", file=sys.stderr)
            if len(issues) > 30:
                print("  additional issues omitted", file=sys.stderr)
            print("No secret values printed. Remove sensitive content from index AND Git history before pushing.", file=sys.stderr)
            return 1
        print(f"SECRET GUARD: PASS ({len(pairs)} file revisions checked)")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"SECRET GUARD: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
