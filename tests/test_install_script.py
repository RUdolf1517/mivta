"""Tests for the Ubuntu/Debian automated installer script (install.sh)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"


def test_install_sh_syntax_and_help() -> None:
    assert INSTALL_SH.is_file()
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must have executable bit set"

    syntax = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_res = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_res.returncode == 0, help_res.stderr
    assert "--token" in help_res.stdout
    assert "--uninstall" in help_res.stdout


def test_install_sh_local_install_idempotent_upgrade_and_uninstall(tmp_path: Path) -> None:
    target_dir = tmp_path / "mivta-opt"
    special_url = "https://openrouter.ai/api/v1?x=1&y=2"
    special_key = r"sk-test_key\with&special/chars"

    res1 = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--from-local",
            str(REPO_ROOT),
            "--dir",
            str(target_dir),
            "--token",
            "987654:TEST_SECRET_TOKEN",
            "--provider",
            "openai",
            "--api-key",
            special_key,
            "--api-url",
            special_url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, f"stdout={res1.stdout}\nstderr={res1.stderr}"

    env_file = target_dir / ".env"
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    # Verify installed .venv parses the generated .env with exact special chars preserved
    verify_py = target_dir / ".venv" / "bin" / "python"
    assert verify_py.is_file()
    check_cfg = subprocess.run(
        [
            str(verify_py),
            "-c",
            (
                "from pathlib import Path; "
                "from mivta.config import Config; "
                f"c = Config.from_env(env={{}}, dotenv_path=Path({str(env_file)!r})); "
                "print(c.bot_token); "
                "print(c.translator_provider); "
                "print(c.translator_api_key); "
                "print(c.translator_api_url); "
                "print(c.database_path)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_cfg.returncode == 0, check_cfg.stderr
    lines = check_cfg.stdout.strip().splitlines()
    assert lines == [
        "987654:TEST_SECRET_TOKEN",
        "openai",
        special_key,
        special_url.rstrip("/"),
        str(target_dir / "data" / "mivta.db"),
    ]

    # Re-run upgrade WITHOUT --token / --api-key: existing .env secrets MUST be preserved!
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k not in {"BOT_TOKEN", "TRANSLATOR_PROVIDER", "TRANSLATOR_API_KEY", "TRANSLATOR_API_URL"}
    }
    res2 = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--from-local",
            str(REPO_ROOT),
            "--dir",
            str(target_dir),
        ],
        env=env_clean,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, f"stdout={res2.stdout}\nstderr={res2.stderr}"
    env_content = env_file.read_text(encoding="utf-8")
    assert "BOT_TOKEN=987654:TEST_SECRET_TOKEN" in env_content
    assert f"TRANSLATOR_API_KEY={special_key}" in env_content

    # Uninstall without --purge preserves target_dir (.env & data/); with --purge deletes it
    res_uninst = subprocess.run(
        ["bash", str(INSTALL_SH), "--no-systemd", "--uninstall", "--dir", str(target_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_uninst.returncode == 0
    assert env_file.is_file()

    res_purge = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--uninstall",
            "--purge",
            "--dir",
            str(target_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_purge.returncode == 0
    assert not target_dir.exists()
