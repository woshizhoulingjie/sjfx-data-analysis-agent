#!/bin/sh
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${SJFX_INSTALL_DIR:-/opt/sjfx}
data_dir=${SJFX_DATA_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/sjfx}
"$package_dir/install_linux.sh"
printf '%s\n' "SJFX upgraded. Previous install is retained as ${install_dir}.backup-*; data remains at ${data_dir}."
