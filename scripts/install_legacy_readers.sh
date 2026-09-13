#!/usr/bin/env bash
# Install CPU-only legacy readers into this project; does not require sudo.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${SJFX_PYTHON:-$project_dir/.venv/bin/python}"
"$python_bin" -m pip install 'xlrd>=2.0.1,<3' 'striprtf>=0.0.29,<0.1' 'olefile>=0.47,<1'
if command -v antiword >/dev/null; then
    exit 0
fi
scratch="$(mktemp -d -t sjfx-legacy-install-XXXXXXXX)"
trap 'rm -rf -- "$scratch"' EXIT
cd "$scratch"
apt-get download antiword
mkdir -p "$project_dir/.tools/legacy-office"
for package in ./*.deb; do
    dpkg-deb -x "$package" "$project_dir/.tools/legacy-office"
done
printf '%s\n' 'Legacy Office readers installed in project .tools/legacy-office.'
