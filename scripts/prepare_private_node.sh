#!/bin/sh
# Download the same Node version used by the Windows frontend regression.
# Usage: sh scripts/prepare_private_node.sh /absolute/private/resource/directory
# This creates files only below that directory; no system installation or PATH edit.
set -eu

node_resource_root=${1:?Provide an absolute private resource directory}
case "$node_resource_root" in
    /*) ;;
    *) printf '%s\n' 'The private resource directory must be absolute.' >&2; exit 2 ;;
esac
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) printf '%s\n' 'This pinned archive is for Linux x86_64.' >&2; exit 2 ;;
esac
[ "$(uname -s)" = Linux ] || { printf '%s\n' 'Linux is required.' >&2; exit 2; }

node_archive_name=node-v24.19.0-linux-x64.tar.xz
node_archive_sha=14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647
node_archive_url=https://nodejs.org/dist/v24.19.0/$node_archive_name
mkdir -p "$node_resource_root"
node_archive_path=$node_resource_root/$node_archive_name

if [ ! -f "$node_archive_path" ]; then
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$node_archive_path.part" "$node_archive_url"
    printf '%s  %s\n' "$node_archive_sha" "$node_archive_path.part" | sha256sum --check --status
    mv "$node_archive_path.part" "$node_archive_path"
fi
printf '%s  %s\n' "$node_archive_sha" "$node_archive_path" | sha256sum --check
tar --extract --xz --file "$node_archive_path" --directory "$node_resource_root" --no-same-owner
node_private_bin=$node_resource_root/node-v24.19.0-linux-x64/bin/node
node_actual_version=$("$node_private_bin" --version)
[ "$node_actual_version" = v24.19.0 ] || { printf '%s\n' 'Unexpected Node version.' >&2; exit 1; }
printf '%s\n' "Node version: $node_actual_version" "Node executable: $node_private_bin" \
    "License: $node_resource_root/node-v24.19.0-linux-x64/LICENSE"
