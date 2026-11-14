"""The public-tree check fails closed when private material is tracked."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_tree.py"
# Split so this file itself contains no 12-digit run.
FAKE_ACCOUNT = "12345678901" + "2"


def _repo(path: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    for rel, body in files.items():
        dest = path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)
    # -f: the fixture must be tracked even when a global exclude would ignore it.
    subprocess.run(["git", "add", "-f", "--", *files], cwd=path, check=True, capture_output=True)
    return path


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"README.md": "public\n"})
    result = _run(repo)
    assert result.returncode == 0
    assert result.stdout == ""


def test_planted_account_id_and_learning_file_fail(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "learning/notes.md": "do not publish\n",
            "src/app.py": f'arn = "arn:aws:iam::{FAKE_ACCOUNT}:role/demo"\n',
        },
    )
    result = _run(repo)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "learning/notes.md:1:" in output
    assert "src/app.py:1:" in output
    assert FAKE_ACCOUNT not in output
    assert "************" in output


@pytest.mark.parametrize(
    "rel",
    [
        "articles/post.md",
        "docs/research/note.md",
        "infra/app/prod.tfvars",
        "infra/app/terraform.tfvars.json",
        "infra/app/prod.auto.tfvars.json",
        "infra/app/terraform.tfstate",
        "infra/app/terraform.tfstate.backup",
        "infra/app/.terraform/providers/x.json",
        ".env",
        "deploy/.env.local",
    ],
)
def test_private_paths_fail(tmp_path: Path, rel: str) -> None:
    repo = _repo(tmp_path, {rel: "x\n"})
    result = _run(repo)
    assert result.returncode != 0
    assert f"{rel}:1:" in result.stdout


@pytest.mark.parametrize("rel", ["infra/app/prod.tfvars.example", ".env.example"])
def test_example_files_pass(tmp_path: Path, rel: str) -> None:
    repo = _repo(tmp_path, {rel: "placeholder\n"})
    result = _run(repo)
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_obvious_secrets_fail_and_are_masked(tmp_path: Path) -> None:
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    token = "sk-" + "or-v1-secretvalue"
    repo = _repo(tmp_path, {"src/keys.py": f'a = "{key}"\nb = "{token}"\n'})
    result = _run(repo)
    assert result.returncode != 0
    assert "src/keys.py:1:" in result.stdout
    assert "src/keys.py:2:" in result.stdout
    assert key not in result.stdout + result.stderr
    assert token not in result.stdout + result.stderr


def test_longer_digit_run_is_not_an_account_id(tmp_path: Path) -> None:
    longer = FAKE_ACCOUNT + "3"
    repo = _repo(tmp_path, {"notes.txt": f"stamp {longer}\n"})
    result = _run(repo)
    assert result.returncode == 0, result.stdout


def test_uv_lock_digest_with_standalone_account_id_fails(tmp_path: Path) -> None:
    digest = "ab" * 32
    line = (
        '{ url = "https://files.pythonhosted.org/packages/ab/cd/x.tar.gz", '
        f'hash = "sha256:{digest}", account = "{FAKE_ACCOUNT}" }}\n'
    )
    repo = _repo(tmp_path, {"uv.lock": line})
    result = _run(repo)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "uv.lock:1:" in result.stdout
    assert FAKE_ACCOUNT not in output
    assert "************" in output


def test_uv_lock_hex_bounded_digits_pass(tmp_path: Path) -> None:
    # Twelve digits inside a digest, including one that starts with them.
    interior = "ab" + "12345678" + "9012" + "cd" * 25
    leading = "12345678" + "9012" + "c" + "ab" * 25 + "d"
    body = f'{{ hash = "sha256:{interior}" }}\n{{ hash = "sha256:{leading}" }}\n'
    repo = _repo(tmp_path, {"uv.lock": body})
    result = _run(repo)
    assert result.returncode == 0, result.stdout


def test_this_repository_passes() -> None:
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout
