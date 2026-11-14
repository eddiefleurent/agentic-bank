"""Fail the build when private material is tracked in this public tree.

Only paths from ``git ls-files`` are scanned, so gitignored local state is out of
scope. A hit prints ``file:line``. Account ids are never copied into the output;
the message uses twelve asterisks instead. Private-path hits do not print file
contents.

Tracked paths that fail:
  learning/, articles/, docs/research/,
  *.tfvars and *.tfvars.json (except *.tfvars.example), *.tfstate*,
  a .terraform/ directory, .env* (except .env.example).

Tracked text that fails:
  a standalone twelve-digit run (an AWS account id, including inside
  ``arn:aws:...:<id>:``), an AKIA access key id, or an OpenRouter key.

Allowlist entries are (path, line pattern) for known-safe twelve-digit runs.
A run is ignored only when the file and the pattern both match. On ``uv.lock``
the run must also sit inside a longer hex token (bounded by ``[0-9a-fA-F]``);
a standalone account id on a digest line still fails. Secrets and private
paths are never allowlisted.
"""

import re
import subprocess
import sys
from pathlib import Path

# Standalone twelve digits, so a longer run (a millisecond timestamp) is not an id.
ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ACCESS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
# Built by concatenation so this file does not itself contain the key prefix.
OPENROUTER = "sk-" + "or-"
OPENROUTER_KEY = re.compile(re.escape(OPENROUTER) + r"\S*")

# File + pattern, not a global digit exception. Each pattern is the safe context
# those digits appear in; a different line in the same file still fails.
ALLOWLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tests/test_worker.py",
        # DynamoDB ARN fixture. Twelve zeros is the usual non-account placeholder.
        re.compile(r"arn:aws:dynamodb:us-east-1:0{12}:table/t"),
    ),
    (
        "uv.lock",
        # PyPI file URLs and sha256 digests. A twelve-digit run on these lines
        # is ignored only when a hex character bounds it, so it is a slice of
        # that token. A standalone account id on the same line still fails.
        re.compile(r"files\.pythonhosted\.org|sha256:"),
    ),
)

MASK = "************"
_HEX = frozenset("0123456789abcdefABCDEF")


def private_path_reason(rel: str) -> str | None:
    name = Path(rel).name
    if rel.startswith("learning/"):
        return "learning/"
    if rel.startswith("articles/"):
        return "articles/"
    if rel.startswith("docs/research/"):
        return "docs/research/"
    if _private_tfvars(name):
        return "*.tfvars"
    if ".tfstate" in name:
        return "*.tfstate*"
    if ".terraform" in rel.split("/"):
        return ".terraform/"
    if name.startswith(".env") and name != ".env.example":
        return ".env*"
    return None


def _private_tfvars(name: str) -> bool:
    if name.endswith(".tfvars.example"):
        return False
    return name.endswith((".tfvars", ".tfvars.json"))


def _part_of_longer_hex(line: str, match: re.Match[str]) -> bool:
    start, end = match.span()
    before = line[start - 1] if start else ""
    after = line[end] if end < len(line) else ""
    return before in _HEX or after in _HEX


def mask_text(text: str) -> str:
    text = ACCESS_KEY.sub("AKIA****************", text)
    text = OPENROUTER_KEY.sub(OPENROUTER + "***", text)
    return ACCOUNT_ID.sub(MASK, text)


def account_id_ignored(rel: str, line: str, match: re.Match[str]) -> bool:
    for path, pattern in ALLOWLIST:
        if path != rel or pattern.search(line) is None:
            continue
        if rel == "uv.lock":
            return _part_of_longer_hex(line, match)
        return True
    return False


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [name.decode() for name in result.stdout.split(b"\0") if name]


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for rel in tracked_files(root):
        where = mask_text(rel)
        reason = private_path_reason(rel)
        if reason is not None:
            findings.append(f"{where}:1: tracked private path ({reason})")
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(not account_id_ignored(rel, line, match) for match in ACCOUNT_ID.finditer(line)):
                findings.append(f"{where}:{lineno}: account id {MASK}")
            if ACCESS_KEY.search(line):
                findings.append(f"{where}:{lineno}: access key")
            if OPENROUTER_KEY.search(line):
                findings.append(f"{where}:{lineno}: openrouter key")
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    root = Path(args[1]) if len(args) > 1 else Path.cwd()
    try:
        findings = scan(root)
    except subprocess.CalledProcessError:
        print("git ls-files failed", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
