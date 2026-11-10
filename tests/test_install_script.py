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


def test_install_sh_auto_update_timer_units_and_rollback_safe_self_update(tmp_path: Path) -> None:
    # 1. Invalid --auto-update-time is rejected immediately
    bad_time = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--from-local",
            str(REPO_ROOT),
            "--dir",
            str(tmp_path / "bad"),
            "--auto-update-time",
            "29:99",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_time.returncode != 0
    assert "invalid auto-update time" in bad_time.stderr

    # 2. Create a local git origin repo so we can test real --self-update & rollback!
    origin_repo = tmp_path / "origin-repo"
    target_dir = tmp_path / "mivta-git-opt"
    systemd_dir = tmp_path / "systemd-units"

    subprocess.run(["git", "init", "-q", "-b", "main", str(origin_repo)], check=True)
    subprocess.run(
        ["git", "-C", str(origin_repo), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(origin_repo), "config", "user.name", "Test"], check=True)
    copied_items = (
        "mivta",
        "pyproject.toml",
        "requirements.txt",
        ".env.example",
        "install.sh",
        "LICENSE",
        "README.md",
    )
    for item in copied_items:
        src_p = REPO_ROOT / item
        dst_p = origin_repo / item
        if src_p.is_dir():
            subprocess.run(["cp", "-R", str(src_p), str(dst_p)], check=True)
        elif src_p.is_file():
            subprocess.run(["cp", str(src_p), str(dst_p)], check=True)
    subprocess.run(["git", "-C", str(origin_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(origin_repo), "commit", "-q", "-m", "initial"], check=True)

    # Clone into target_dir so target_dir/.git tracks origin_repo
    subprocess.run(["git", "clone", "-q", str(origin_repo), str(target_dir)], check=True)

    env_with_systemd_dir = {**os.environ, "MIVTA_SYSTEMD_DIR": str(systemd_dir)}
    inst = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--from-local",
            str(target_dir),
            "--dir",
            str(target_dir),
            "--token",
            "123456:AUTO_UPD_TOKEN",
            "--auto-update-time",
            "6:00",
        ],
        env=env_with_systemd_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert inst.returncode == 0, f"stdout={inst.stdout}\nstderr={inst.stderr}"

    timer_file = systemd_dir / "mivta-update.timer"
    upd_svc_file = systemd_dir / "mivta-update.service"
    assert timer_file.is_file()
    assert upd_svc_file.is_file()
    assert "OnCalendar=*-*-* 06:00:00" in timer_file.read_text(encoding="utf-8")
    assert "--self-update" in upd_svc_file.read_text(encoding="utf-8")

    self_upd_cmd = [
        "bash",
        str(target_dir / "install.sh"),
        "--no-systemd",
        "--self-update",
        "--dir",
        str(target_dir),
    ]

    # 3. --self-update when already up to date -> fast no-op
    noop = subprocess.run(self_upd_cmd, capture_output=True, text=True, check=False)
    assert noop.returncode == 0, noop.stderr
    assert "Already up to date" in noop.stdout

    # 4. Push a valid new commit to origin_repo -> --self-update fast-forwards & preserves .env
    marker_file = origin_repo / "mivta" / "_marker.py"
    marker_file.write_text("UPDATED = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(origin_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(origin_repo), "commit", "-q", "-m", "v2"], check=True)
    good_rev = subprocess.run(
        ["git", "-C", str(origin_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    upd_ok = subprocess.run(self_upd_cmd, capture_output=True, text=True, check=False)
    assert upd_ok.returncode == 0, f"stdout={upd_ok.stdout}\nstderr={upd_ok.stderr}"
    assert (target_dir / "mivta" / "_marker.py").is_file()
    assert "BOT_TOKEN=123456:AUTO_UPD_TOKEN" in (target_dir / ".env").read_text(encoding="utf-8")

    # 5. Push a broken commit (syntax error in bot.py) -> --self-update rolls back to good_rev
    (origin_repo / "mivta" / "bot.py").write_text("def broken(:\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(origin_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(origin_repo), "commit", "-q", "-m", "broken"], check=True)

    upd_rollback = subprocess.run(self_upd_cmd, capture_output=True, text=True, check=False)
    assert upd_rollback.returncode == 0, upd_rollback.stderr
    assert "rolling back" in upd_rollback.stderr
    current_rev = subprocess.run(
        ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current_rev == good_rev

    # 6. --no-auto-update removes the timer & update service units cleanly
    disable_res = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--no-auto-update",
            "--from-local",
            str(REPO_ROOT),
            "--dir",
            str(target_dir),
        ],
        env=env_with_systemd_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert disable_res.returncode == 0, disable_res.stderr
    assert not timer_file.exists()
    assert not upd_svc_file.exists()


def test_install_sh_privilege_separation_permissions(tmp_path: Path) -> None:
    """Verify root:root (0755), root:mivta (0640), and mivta:mivta (0700) separation."""
    install_dir = tmp_path / "opt-mivta"
    (install_dir / "mivta").mkdir(parents=True)
    (install_dir / ".venv" / "bin").mkdir(parents=True)
    (install_dir / ".git").mkdir(parents=True)
    (install_dir / "data").mkdir(parents=True)

    # Seed permissive initial modes to ensure apply_install_permissions tightens them
    (install_dir / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (install_dir / "install.sh").chmod(0o777)
    (install_dir / "mivta" / "bot.py").write_text("x = 1\n", encoding="utf-8")
    (install_dir / "mivta" / "bot.py").chmod(0o666)
    (install_dir / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (install_dir / ".venv" / "bin" / "python").chmod(0o777)
    (install_dir / "README.md").write_text("# doc\n", encoding="utf-8")
    (install_dir / "README.md").chmod(0o666)
    (install_dir / ".env").write_text("BOT_TOKEN=1:secret\n", encoding="utf-8")
    (install_dir / ".env").chmod(0o666)
    (install_dir / "data" / "mivta.db").write_text("db", encoding="utf-8")
    (install_dir / "data" / "mivta.db").chmod(0o666)

    chown_log = tmp_path / "chown.log"
    # Extract apply_install_permissions from install.sh and run under simulated root + systemd
    harness = tmp_path / "run_perms.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'INSTALL_DIR="{install_dir}"\n'
        'SERVICE_USER="mivta"\n'
        'NO_SYSTEMD="0"\n'
        "is_root() { return 0; }\n"
        "id() { return 0; }\n"
        f'chown() {{ printf "%s\\n" "$*" >> "{chown_log}"; }}\n'
        # Extract the exact function body from install.sh
        f'eval "$(awk \'/^apply_install_permissions\\(\\)/,/^}}/\' "{INSTALL_SH}")"\n'
        "apply_install_permissions\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"

    logged = chown_log.read_text(encoding="utf-8").splitlines()
    assert logged == [
        f"-R root:root {install_dir}",
        f"root:mivta {install_dir}/.env",
        f"-R mivta:mivta {install_dir}/data",
    ]

    assert stat.S_IMODE(install_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((install_dir / "install.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((install_dir / "mivta" / "bot.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((install_dir / ".venv" / "bin" / "python").stat().st_mode) == 0o755
    assert stat.S_IMODE((install_dir / "README.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((install_dir / ".git").stat().st_mode) == 0o700
    assert stat.S_IMODE((install_dir / ".env").stat().st_mode) == 0o640
    assert stat.S_IMODE((install_dir / "data").stat().st_mode) == 0o700
    assert stat.S_IMODE((install_dir / "data" / "mivta.db").stat().st_mode) == 0o600
