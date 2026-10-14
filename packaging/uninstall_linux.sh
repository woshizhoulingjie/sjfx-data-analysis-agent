#!/bin/sh
set -eu

install_dir=${SJFX_INSTALL_DIR:-/opt/sjfx}
data_dir=${SJFX_DATA_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/sjfx}
guard_path() {
  case "$1" in /*) ;; *) echo "Removal paths must be absolute: $1" >&2; exit 1 ;; esac
  path=$(realpath -m -- "$1") || { echo "Unable to resolve path: $1" >&2; exit 1; }
  case "$path" in
    ''|/|/home|/root|/tmp|/var|/usr|/opt|/etc|/bin|/sbin|/lib|/lib64|"$HOME")
      echo "Refusing to remove unsafe path: $path" >&2; exit 1 ;;
  esac
  printf '%s\n' "$path"
}
install_dir=$(guard_path "$install_dir")
data_dir=$(guard_path "$data_dir")
case "$data_dir/" in
  "$install_dir/"*)
    echo "Install and data paths must not overlap: $install_dir and $data_dir" >&2
    exit 1
    ;;
esac
case "$install_dir/" in
  "$data_dir/"*)
    echo "Install and data paths must not overlap: $install_dir and $data_dir" >&2
    exit 1
    ;;
esac
action=${1:-keep-data}
case "$action" in
  keep-data|purge-data) ;;
  *)
    echo "usage: $0 [keep-data|purge-data]" >&2
    exit 2
    ;;
esac
make_removable() {
  target=$1
  [ -e "$target" ] || return 0
  # The installer removes write bits from the immutable application tree.
  # Restore only the owner's bits so an ordinary user can uninstall it.
  find "$target" -type d -exec chmod u+rwx {} + 2>/dev/null || true
  find "$target" -type f -exec chmod u+rw {} + 2>/dev/null || true
}
if [ -d "$install_dir" ]; then
  env_file="$data_dir/config/sjfx.env"
  python="$install_dir/runtime/bin/python3"
  controller="$install_dir/app/sjfxctl.py"
  if [ -f "$env_file" ] && [ -x "$python" ] && [ -f "$controller" ]; then
    if ! "$python" -B "$controller" --env-file "$env_file" stop --timeout 30; then
      echo "The SJFX instance did not stop cleanly; refusing to remove $install_dir" >&2
      exit 1
    fi
  fi
fi
case "$action" in
  keep-data)
    make_removable "$install_dir"
    rm -rf -- "$install_dir"
    printf '%s\n' "SJFX program files removed; user data retained at $data_dir."
    ;;
  purge-data)
    make_removable "$install_dir"
    make_removable "$data_dir"
    rm -rf -- "$install_dir" "$data_dir"
    printf '%s\n' "SJFX program files and user data removed."
    ;;
esac
