#!/usr/bin/env bash
# mivta — automated installer & upgrader for Ubuntu (22.04 / 24.04 / 26.04+) and Debian:
#
#   curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
#
# Or non-interactively with configuration variables:
#
#   curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh \
#     | sudo BOT_TOKEN="123456:ABC..." TRANSLATOR_PROVIDER="google" bash
#
# Re-running the script updates the code and Python dependencies in place while
# preserving /opt/mivta/.env and /opt/mivta/data/mivta.db.

set -euo pipefail

SERVICE_NAME="${MIVTA_SERVICE_NAME:-mivta}"
INSTALL_DIR="${MIVTA_INSTALL_DIR:-/opt/mivta}"
SERVICE_USER="${MIVTA_USER:-mivta}"
REPO="${MIVTA_REPO:-RUdolf1517/mivta}"
BRANCH="${MIVTA_BRANCH:-main}"
SOURCE_DIR="${MIVTA_SOURCE_DIR:-}"
NONINTERACTIVE="${MIVTA_NONINTERACTIVE:-0}"
NO_SYSTEMD="${MIVTA_NO_SYSTEMD:-0}"
ACTION="install"
PURGE=0

ARG_BOT_TOKEN="${BOT_TOKEN:-}"
ARG_PROVIDER="${TRANSLATOR_PROVIDER:-}"
ARG_API_KEY="${TRANSLATOR_API_KEY:-}"
ARG_API_URL="${TRANSLATOR_API_URL:-}"
ARG_MODEL="${TRANSLATOR_MODEL:-}"
ARG_FALLBACK_PROVIDER="${TRANSLATOR_FALLBACK_PROVIDER:-}"
ARG_GLOSSARY="${TRANSLATOR_GLOSSARY:-}"
ARG_TARGET_LANG="${DEFAULT_TARGET_LANG:-}"
ARG_SOURCE_LANG="${DEFAULT_SOURCE_LANG:-}"
ARG_STYLE="${DEFAULT_STYLE:-}"
ARG_INLINE_EXTRA_LANGS="${INLINE_EXTRA_LANGS:-}"
ARG_INLINE_STYLE_VARIANTS="${INLINE_STYLE_VARIANTS:-}"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'mivta-install: warning: %s\n' "$*" >&2; }
err()  { printf 'mivta-install: error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

Automated Ubuntu/Debian installer and upgrader for mivta.

Options:
  --token <TOKEN>          Telegram BOT_TOKEN from @BotFather
  --provider <NAME>        google | openai | groq | gemini | openrouter | deepl | libretranslate
  --api-key <KEY>          TRANSLATOR_API_KEY (for openai / groq / gemini / openrouter / deepl)
  --api-url <URL>          Custom TRANSLATOR_API_URL
  --model <MODEL>          TRANSLATOR_MODEL for LLM provider
  --dir <PATH>             Install directory (default: /opt/mivta)
  --branch <BRANCH>        Git branch or tag to install (default: main)
  --from-local [DIR]       Install from local checkout instead of cloning GitHub
  --non-interactive        Never prompt on /dev/tty
  --no-systemd             Skip creating/managing systemd service (e.g. containers)
  --uninstall              Stop and remove systemd service (keeps .env and data/)
  --purge                  With --uninstall, also delete install dir and system user
  -h, --help               Show this help message

Environment variables:
  BOT_TOKEN, TRANSLATOR_PROVIDER, TRANSLATOR_API_KEY, TRANSLATOR_API_URL,
  TRANSLATOR_MODEL, TRANSLATOR_FALLBACK_PROVIDER, TRANSLATOR_GLOSSARY,
  DEFAULT_TARGET_LANG, DEFAULT_SOURCE_LANG, DEFAULT_STYLE,
  INLINE_EXTRA_LANGS, INLINE_STYLE_VARIANTS,
  MIVTA_INSTALL_DIR, MIVTA_USER, MIVTA_REPO, MIVTA_BRANCH, MIVTA_NONINTERACTIVE=1
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token)
      [ $# -ge 2 ] || err "--token requires a value"
      ARG_BOT_TOKEN="$2"
      shift 2
      ;;
    --provider)
      [ $# -ge 2 ] || err "--provider requires a value"
      ARG_PROVIDER="$2"
      shift 2
      ;;
    --api-key)
      [ $# -ge 2 ] || err "--api-key requires a value"
      ARG_API_KEY="$2"
      shift 2
      ;;
    --api-url)
      [ $# -ge 2 ] || err "--api-url requires a value"
      ARG_API_URL="$2"
      shift 2
      ;;
    --model)
      [ $# -ge 2 ] || err "--model requires a value"
      ARG_MODEL="$2"
      shift 2
      ;;
    --dir)
      [ $# -ge 2 ] || err "--dir requires a value"
      INSTALL_DIR="$2"
      shift 2
      ;;
    --branch)
      [ $# -ge 2 ] || err "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --from-local)
      if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then
        SOURCE_DIR="$2"
        shift 2
      else
        SOURCE_DIR="$(pwd)"
        shift
      fi
      ;;
    --non-interactive)
      NONINTERACTIVE=1
      shift
      ;;
    --no-systemd)
      NO_SYSTEMD=1
      shift
      ;;
    --uninstall)
      ACTION="uninstall"
      shift
      ;;
    --purge)
      PURGE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "unknown argument: $1 (try --help)"
      ;;
  esac
done

is_root() {
  [ "$(id -u)" -eq 0 ]
}

check_platform() {
  [ "$(uname -s)" = "Linux" ] || err "this installer targets Ubuntu/Debian Linux (got $(uname -s))"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-} ${ID_LIKE:-}" in
      *ubuntu*|*debian*) ;;
      *) warn "tested on Ubuntu/Debian (detected ID=${ID:-unknown}); continuing" ;;
    esac
  fi
}

python_meets_min_version() {
  local py="$1"
  "$py" -c 'import sys, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_suitable_python() {
  local candidate
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if need "$candidate" && python_meets_min_version "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get \
    -o DPkg::Lock::Timeout=60 \
    install -y --no-install-recommends "$@"
}

ensure_dependencies() {
  local missing_pkgs=()
  need git || missing_pkgs+=(git)
  need curl || missing_pkgs+=(curl)
  [ -f /etc/ssl/certs/ca-certificates.crt ] || missing_pkgs+=(ca-certificates)

  local py_bin=""
  py_bin="$(find_suitable_python || true)"

  if [ -n "$py_bin" ] && [ "${#missing_pkgs[@]}" -eq 0 ]; then
    printf '%s\n' "$py_bin"
    return 0
  fi

  is_root || err "missing system packages (python3.11+ with venv, git, ca-certificates); re-run with sudo"
  need apt-get || err "apt-get not found; install Python >= 3.11 (with venv/ensurepip) and git manually"

  say "Updating apt package index..." >&2
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 update -qq >&2

  if [ -z "$py_bin" ]; then
    # Check whether default python3 is >= 3.11 (Ubuntu 24.04+ / Debian 12+) or < 3.11 (Ubuntu 22.04)
    if need python3 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      local py_minor
      py_minor="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      missing_pkgs+=(python3 "python${py_minor}-venv" python3-venv)
    elif apt-cache show python3.12 >/dev/null 2>&1 && apt-cache show python3.12-venv >/dev/null 2>&1; then
      missing_pkgs+=(python3.12 python3.12-venv)
    elif apt-cache show python3.11 >/dev/null 2>&1 && apt-cache show python3.11-venv >/dev/null 2>&1; then
      missing_pkgs+=(python3.11 python3.11-venv)
    else
      err "could not find Python >= 3.11 in apt repositories (enable Ubuntu 'universe' repository and retry)"
    fi
  fi

  say "Installing system packages: ${missing_pkgs[*]}..." >&2
  apt_install "${missing_pkgs[@]}" >&2

  py_bin="$(find_suitable_python || true)"
  [ -n "$py_bin" ] || err "Python >= 3.11 with venv/ensurepip is still unavailable after apt install"
  printf '%s\n' "$py_bin"
}

# Safely set or replace KEY=VALUE inside an env file (via ENVIRON so backslashes/URLs stay literal).
set_env_var() {
  local env_file="$1"
  local key="$2"
  local val="$3"
  local tmp_file="${env_file}.tmp.$$"

  ENV_KEY="$key" ENV_VAL="$val" awk '
    BEGIN {
      k = ENVIRON["ENV_KEY"]
      v = ENVIRON["ENV_VAL"]
      updated = 0
    }
    index($0, k "=") == 1 {
      print k "=" v
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print k "=" v
      }
    }
  ' "$env_file" > "$tmp_file"
  mv "$tmp_file" "$env_file"
}

get_env_var() {
  local env_file="$1"
  local key="$2"
  [ -f "$env_file" ] || return 0
  ENV_KEY="$key" awk '
    BEGIN { k = ENVIRON["ENV_KEY"] }
    index($0, k "=") == 1 {
      val = substr($0, length(k) + 2)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      if ((val ~ /^".*"$/) || (val ~ /^\x27.*\x27$/)) {
        val = substr(val, 2, length(val) - 2)
      }
      print val
    }
  ' "$env_file" | tail -n 1
}

token_looks_configured() {
  local tok="$1"
  [ -n "$tok" ] || return 1
  case "$tok" in
    *REPLACE_WITH_YOUR_BOT_TOKEN*|*YOUR_BOT_TOKEN*) return 1 ;;
    *:*) return 0 ;;
    *) return 1 ;;
  esac
}

do_uninstall() {
  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  if [ "$NO_SYSTEMD" != "1" ]; then
    is_root || err "--uninstall requires root privileges (run with sudo)"
    if [ -d /run/systemd/system ] && need systemctl; then
      if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
        say "Stopping and disabling ${SERVICE_NAME}.service..."
        systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
      fi
    fi
    if [ -f "$unit_path" ]; then
      rm -f "$unit_path"
      if [ -d /run/systemd/system ] && need systemctl; then
        systemctl daemon-reload
      fi
      say "Removed $unit_path"
    fi
  fi

  if [ "$PURGE" = "1" ]; then
    if [ -d "$INSTALL_DIR" ]; then
      rm -rf "$INSTALL_DIR"
      say "Removed $INSTALL_DIR"
    fi
    if is_root && id -u "$SERVICE_USER" >/dev/null 2>&1; then
      userdel "$SERVICE_USER" 2>/dev/null || true
      say "Removed system user $SERVICE_USER"
    fi
  else
    say "Kept $INSTALL_DIR (.env and data/ preserved). Pass --uninstall --purge to remove everything."
  fi
}

sync_source() {
  mkdir -p "$INSTALL_DIR"

  if [ -n "$SOURCE_DIR" ]; then
    local src_abs
    src_abs="$(cd "$SOURCE_DIR" && pwd)"
    [ -f "$src_abs/pyproject.toml" ] || err "--from-local directory $src_abs does not contain pyproject.toml"
    [ -d "$src_abs/mivta" ] || err "--from-local directory $src_abs does not contain mivta/ package"

    if [ "$src_abs" != "$(cd "$INSTALL_DIR" && pwd)" ]; then
      say "Copying project files from $src_abs to $INSTALL_DIR..."
      rm -rf "$INSTALL_DIR/mivta"
      cp -R "$src_abs/mivta" "$INSTALL_DIR/mivta"
      for f in pyproject.toml requirements.txt .env.example install.sh LICENSE README.md CHANGELOG.md; do
        [ -f "$src_abs/$f" ] && cp "$src_abs/$f" "$INSTALL_DIR/$f"
      done
      [ -f "$INSTALL_DIR/install.sh" ] && chmod 0755 "$INSTALL_DIR/install.sh"
    fi
    return 0
  fi

  local repo_url="https://github.com/${REPO}.git"
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating existing repository in $INSTALL_DIR (branch: $BRANCH)..."
    git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" remote set-url origin "$repo_url"
    git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
    git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" checkout -f FETCH_HEAD
  else
    local tmp_clone
    tmp_clone="$(mktemp -d)"
    say "Cloning $repo_url ($BRANCH)..."
    if ! git clone --depth 1 --branch "$BRANCH" "$repo_url" "$tmp_clone/repo"; then
      rm -rf "$tmp_clone"
      err "failed to clone $repo_url (branch $BRANCH)"
    fi
    cp -a "$tmp_clone/repo/." "$INSTALL_DIR/"
    rm -rf "$tmp_clone"
  fi
}

configure_env_file() {
  local env_file="$INSTALL_DIR/.env"
  if [ ! -f "$env_file" ]; then
    say "Creating $env_file from .env.example..."
    cp "$INSTALL_DIR/.env.example" "$env_file"
  else
    say "Preserving existing $env_file..."
  fi
  chmod 0600 "$env_file"

  set_env_var "$env_file" "DATABASE_PATH" "${INSTALL_DIR}/data/mivta.db"

  [ -n "$ARG_BOT_TOKEN" ]             && set_env_var "$env_file" "BOT_TOKEN" "$ARG_BOT_TOKEN"
  [ -n "$ARG_PROVIDER" ]              && set_env_var "$env_file" "TRANSLATOR_PROVIDER" "$ARG_PROVIDER"
  [ -n "$ARG_API_KEY" ]               && set_env_var "$env_file" "TRANSLATOR_API_KEY" "$ARG_API_KEY"
  [ -n "$ARG_API_URL" ]               && set_env_var "$env_file" "TRANSLATOR_API_URL" "$ARG_API_URL"
  [ -n "$ARG_MODEL" ]                 && set_env_var "$env_file" "TRANSLATOR_MODEL" "$ARG_MODEL"
  [ -n "$ARG_FALLBACK_PROVIDER" ]     && set_env_var "$env_file" "TRANSLATOR_FALLBACK_PROVIDER" "$ARG_FALLBACK_PROVIDER"
  [ -n "$ARG_GLOSSARY" ]              && set_env_var "$env_file" "TRANSLATOR_GLOSSARY" "$ARG_GLOSSARY"
  [ -n "$ARG_TARGET_LANG" ]           && set_env_var "$env_file" "DEFAULT_TARGET_LANG" "$ARG_TARGET_LANG"
  [ -n "$ARG_SOURCE_LANG" ]           && set_env_var "$env_file" "DEFAULT_SOURCE_LANG" "$ARG_SOURCE_LANG"
  [ -n "$ARG_STYLE" ]                 && set_env_var "$env_file" "DEFAULT_STYLE" "$ARG_STYLE"
  [ -n "$ARG_INLINE_EXTRA_LANGS" ]    && set_env_var "$env_file" "INLINE_EXTRA_LANGS" "$ARG_INLINE_EXTRA_LANGS"
  [ -n "$ARG_INLINE_STYLE_VARIANTS" ] && set_env_var "$env_file" "INLINE_STYLE_VARIANTS" "$ARG_INLINE_STYLE_VARIANTS"

  local current_tok
  current_tok="$(get_env_var "$env_file" "BOT_TOKEN")"

  # Prompt interactively via /dev/tty (works even under `curl ... | sudo bash`)
  if ! token_looks_configured "$current_tok" && [ "$NONINTERACTIVE" != "1" ] && [ -t 1 ] && [ -r /dev/tty ]; then
    printf '\nEnter Telegram BOT_TOKEN from @BotFather (or press Enter to configure later): ' >/dev/tty
    local input_tok=""
    IFS= read -r input_tok </dev/tty || true
    input_tok="${input_tok#"${input_tok%%[![:space:]]*}"}"
    input_tok="${input_tok%"${input_tok##*[![:space:]]}"}"
    if [ -n "$input_tok" ]; then
      set_env_var "$env_file" "BOT_TOKEN" "$input_tok"
      current_tok="$input_tok"
    fi

    if [ -z "$ARG_PROVIDER" ]; then
      printf 'Translator provider [google/openai/groq/gemini/openrouter/deepl/libretranslate] (default: google): ' >/dev/tty
      local input_prov=""
      IFS= read -r input_prov </dev/tty || true
      input_prov="${input_prov#"${input_prov%%[![:space:]]*}"}"
      input_prov="${input_prov%"${input_prov##*[![:space:]]}"}"
      if [ -n "$input_prov" ]; then
        set_env_var "$env_file" "TRANSLATOR_PROVIDER" "$input_prov"
        case "$input_prov" in
          openai|groq|gemini|openrouter|deepl)
            printf 'Enter TRANSLATOR_API_KEY for %s: ' "$input_prov" >/dev/tty
            local input_key=""
            IFS= read -r input_key </dev/tty || true
            input_key="${input_key#"${input_key%%[![:space:]]*}"}"
            input_key="${input_key%"${input_key##*[![:space:]]}"}"
            [ -n "$input_key" ] && set_env_var "$env_file" "TRANSLATOR_API_KEY" "$input_key"
            ;;
        esac
      fi
    fi
  fi

  chmod 0600 "$env_file"
}

install_systemd_service() {
  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  say "Writing systemd service unit $unit_path..."
  cat > "$unit_path" <<EOF
[Unit]
Description=mivta Telegram Inline & Business Translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/python -m mivta
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/data

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$unit_path"

  if [ ! -d /run/systemd/system ] || ! need systemctl; then
    warn "systemd is not running as PID 1 on this host; unit file written, skipping systemctl start"
    say "Run manually with: ${INSTALL_DIR}/.venv/bin/python -m mivta"
    return 0
  fi

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service" >/dev/null

  local env_file="$INSTALL_DIR/.env"
  local current_tok
  current_tok="$(get_env_var "$env_file" "BOT_TOKEN")"

  if token_looks_configured "$current_tok"; then
    # Verify configuration parses cleanly before starting/restarting the daemon
    if ! "$INSTALL_DIR/.venv/bin/python" -c '
import sys
from pathlib import Path
from mivta.config import Config
Config.from_env(env={}, dotenv_path=Path(sys.argv[1]))
' "$env_file" >/dev/null; then
      err "configuration in $env_file is invalid; fix it and run: sudo systemctl restart ${SERVICE_NAME}"
    fi

    say "Starting ${SERVICE_NAME}.service..."
    systemctl restart "${SERVICE_NAME}.service"
    sleep 1
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
      say "${SERVICE_NAME}.service is active and running."
    else
      warn "${SERVICE_NAME}.service exited early; check logs with: sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
      exit 1
    fi
  else
    warn "BOT_TOKEN is not configured yet in $env_file; service enabled but not started."
    printf '\nNext steps:\n'
    printf '  1. sudo nano %s\n' "$env_file"
    printf '  2. sudo systemctl start %s\n' "$SERVICE_NAME"
    printf '  3. sudo journalctl -u %s -f\n' "$SERVICE_NAME"
  fi
}

main() {
  check_platform

  if [ "$ACTION" = "uninstall" ]; then
    do_uninstall
    exit 0
  fi

  if [ "$NO_SYSTEMD" != "1" ] && ! is_root; then
    err "system-wide Ubuntu install requires root privileges (re-run with sudo, or pass --no-systemd --dir ~/.local/share/mivta)"
  fi

  local py_bin
  py_bin="$(ensure_dependencies)"
  say "Using Python interpreter: $py_bin ($("$py_bin" --version 2>&1))"

  if [ "$NO_SYSTEMD" != "1" ] && is_root; then
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
      say "Creating dedicated system user '$SERVICE_USER'..."
      useradd --system --user-group --home-dir "$INSTALL_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
  fi

  sync_source
  mkdir -p "$INSTALL_DIR/data"
  chmod 0750 "$INSTALL_DIR/data"

  say "Setting up virtual environment in $INSTALL_DIR/.venv..."
  if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
    "$py_bin" -m venv "$INSTALL_DIR/.venv"
  fi
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  say "Installing Python dependencies..."
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
  "$INSTALL_DIR/.venv/bin/pip" install -q --no-deps "$INSTALL_DIR"

  # Verify imports work on the target machine before wiring up the service
  if ! "$INSTALL_DIR/.venv/bin/python" -c "import mivta.bot, mivta.config, mivta.database, mivta.translator" >/dev/null; then
    err "post-install verification failed: could not import mivta modules"
  fi

  configure_env_file

  if [ "$NO_SYSTEMD" != "1" ] && is_root; then
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
    chmod 0600 "$INSTALL_DIR/.env"
    install_systemd_service
  else
    say "Installed mivta into $INSTALL_DIR (--no-systemd mode)."
    say "Start with: (cd $INSTALL_DIR && $INSTALL_DIR/.venv/bin/python -m mivta)"
  fi

  printf '\nDone! Useful commands:\n'
  if [ "$NO_SYSTEMD" != "1" ]; then
    printf '  Status:   sudo systemctl status %s\n' "$SERVICE_NAME"
    printf '  Logs:     sudo journalctl -u %s -f\n' "$SERVICE_NAME"
    printf '  Config:   sudo nano %s/.env && sudo systemctl restart %s\n' "$INSTALL_DIR" "$SERVICE_NAME"
    printf '  Upgrade:  curl -fsSL https://raw.githubusercontent.com/%s/%s/install.sh | sudo bash\n' "$REPO" "$BRANCH"
  else
    printf '  Config:   nano %s/.env\n' "$INSTALL_DIR"
    printf '  Run:      (cd %s && %s/.venv/bin/python -m mivta)\n' "$INSTALL_DIR" "$INSTALL_DIR"
  fi
}

main "$@"
