#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
KLIPPER_EXTRAS_DIR="${KLIPPER_PATH}/klippy/extras"
MOONRAKER_SERVICE_NAME="${MOONRAKER_SERVICE_NAME:-moonraker}"

find_config_dir() {
  if [[ -n "${PRINTER_CONFIG_DIR:-}" && -d "${PRINTER_CONFIG_DIR}" ]]; then
    printf '%s\n' "${PRINTER_CONFIG_DIR}"
    return
  fi
  if [[ -d "$HOME/printer_data/config" ]]; then
    printf '%s\n' "$HOME/printer_data/config"
    return
  fi
  if [[ -d "$HOME/klipper_config" ]]; then
    printf '%s\n' "$HOME/klipper_config"
    return
  fi
  printf '%s\n' "$HOME/printer_data/config"
}

find_moonraker_conf() {
  if [[ -n "${MOONRAKER_CONF:-}" && -f "${MOONRAKER_CONF}" ]]; then
    printf '%s\n' "${MOONRAKER_CONF}"
    return
  fi

  # Parse active moonraker process command line for "-c /path/to/moonraker.conf"
  # shellcheck disable=SC2009
  cmdline="$(ps -eo args | grep -E '[m]oonraker(\.py)?' | head -n 1 || true)"
  if [[ -n "$cmdline" ]]; then
    conf_path="$(sed -n 's/.*[[:space:]]-c[[:space:]]\([^[:space:]]\+\).*/\1/p' <<<"$cmdline" | head -n 1 || true)"
    if [[ -n "$conf_path" && -f "$conf_path" ]]; then
      printf '%s\n' "$conf_path"
      return
    fi
  fi

  # Parse systemd unit for config path if process parsing failed
  if command -v systemctl >/dev/null 2>&1; then
    execstart="$(systemctl show "$MOONRAKER_SERVICE_NAME" -p ExecStart --value 2>/dev/null || true)"
    if [[ -n "$execstart" ]]; then
      conf_path="$(sed -n 's/.*[[:space:]]-c[[:space:]]\([^[:space:]]\+\).*/\1/p' <<<"$execstart" | head -n 1 || true)"
      if [[ -n "$conf_path" && -f "$conf_path" ]]; then
        printf '%s\n' "$conf_path"
        return
      fi
    fi
  fi

  for p in \
    "$HOME/printer_data/config/moonraker.conf" \
    "$HOME/klipper_config/moonraker.conf" \
    "$HOME/klipper_config/moonraker/moonraker.conf"; do
    if [[ -f "$p" ]]; then
      printf '%s\n' "$p"
      return
    fi
  done

  printf '%s\n' "$CONFIG_DIR/moonraker.conf"
}

CONFIG_DIR="$(find_config_dir)"
mkdir -p "$CONFIG_DIR"

if [[ ! -d "$KLIPPER_EXTRAS_DIR" ]]; then
  echo "ERROR: Klipper extras directory not found at: $KLIPPER_EXTRAS_DIR"
  echo "Set KLIPPER_PATH to your Klipper checkout and rerun install.sh"
  exit 1
fi

PLUGIN_SRC="$REPO_DIR/klippy/extras/auto_z_tap.py"
PLUGIN_DST="$KLIPPER_EXTRAS_DIR/auto_z_tap.py"

if [[ ! -f "$PLUGIN_SRC" ]]; then
  echo "ERROR: Missing plugin source: $PLUGIN_SRC"
  exit 1
fi

echo "Using KLIPPER_PATH: $KLIPPER_PATH"
echo "Using CONFIG_DIR: $CONFIG_DIR"

ln -sf "$PLUGIN_SRC" "$PLUGIN_DST"
echo "Linked plugin: $PLUGIN_DST -> $PLUGIN_SRC"

SAMPLE_CFG_SRC="$REPO_DIR/examples/auto_z_tap.cfg"
SAMPLE_CFG_DST="$CONFIG_DIR/auto_z_tap.cfg"
if [[ -f "$SAMPLE_CFG_SRC" && ! -f "$SAMPLE_CFG_DST" ]]; then
  cp "$SAMPLE_CFG_SRC" "$SAMPLE_CFG_DST"
  echo "Installed sample config: $SAMPLE_CFG_DST"
fi

origin_url="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
if [[ -z "$origin_url" ]]; then
  # Fallback to first available remote URL before a placeholder.
  first_remote="$(git -C "$REPO_DIR" remote 2>/dev/null | head -n 1 || true)"
  if [[ -n "$first_remote" ]]; then
    origin_url="$(git -C "$REPO_DIR" remote get-url "$first_remote" 2>/dev/null || true)"
  fi
fi
if [[ -z "$origin_url" ]]; then
  origin_url="https://github.com/REPLACE_ME/auto-z.git"
  echo "WARNING: Could not determine git remote URL for $REPO_DIR"
  echo "WARNING: Update manager may not track updates until you set 'origin' manually."
fi

branch_name="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ -z "$branch_name" || "$branch_name" == "HEAD" ]]; then
  branch_name="main"
fi

UPDATE_FILE="$CONFIG_DIR/auto_z_tap_update_manager.conf"
cat > "$UPDATE_FILE" <<EOF
[update_manager auto_z_tap]
type: git_repo
channel: dev
path: $REPO_DIR
origin: $origin_url
primary_branch: $branch_name
managed_services: klipper
install_script: $REPO_DIR/install.sh
EOF

echo "Wrote Moonraker update-manager snippet: $UPDATE_FILE"

MOONRAKER_CONF="$(find_moonraker_conf)"
mkdir -p "$(dirname "$MOONRAKER_CONF")"
INCLUDE_LINE="[include auto_z_tap_update_manager.conf]"
if [[ -f "$MOONRAKER_CONF" ]]; then
  if ! grep -Fqi "$INCLUDE_LINE" "$MOONRAKER_CONF"; then
    printf "\n%s\n" "$INCLUDE_LINE" >> "$MOONRAKER_CONF"
    echo "Appended include to $MOONRAKER_CONF"
  fi
else
  cat > "$MOONRAKER_CONF" <<EOF
$INCLUDE_LINE
EOF
  echo "Created $MOONRAKER_CONF with update-manager include"
fi

echo "Using MOONRAKER_CONF: $MOONRAKER_CONF"
echo "Install finished. Restart Klipper and Moonraker to load AUTO_Z_TAP."
