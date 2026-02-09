#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
KLIPPER_EXTRAS_DIR="${KLIPPER_PATH}/klippy/extras"

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
  origin_url="https://github.com/REPLACE_ME/auto-z.git"
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

MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"
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

echo "Install finished. Restart Klipper and Moonraker to load AUTO_Z_TAP."
