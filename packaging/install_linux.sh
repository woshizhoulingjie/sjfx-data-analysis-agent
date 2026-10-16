#!/bin/sh
set -eu
umask 077

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${SJFX_INSTALL_DIR:-/opt/sjfx}
data_dir=${SJFX_DATA_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/sjfx}
guard_path() {
    case "$1" in
        /*) ;;
        *) echo "Install and data paths must be absolute: $1" >&2; exit 1 ;;
    esac
    path=$(realpath -m -- "$1") || { echo "Unable to resolve path: $1" >&2; exit 1; }
    case "$path" in
        ''|/|/home|/root|/tmp|/var|/usr|/opt|/etc|/bin|/sbin|/lib|/lib64|"$HOME")
            echo "Refusing to use unsafe install or data path: $path" >&2; exit 1 ;;
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
for name in app runtime models tools manifest.json; do
    [ -e "$package_dir/$name" ] || { echo "Offline bundle is incomplete: $name" >&2; exit 1; }
done
[ -x "$package_dir/runtime/bin/python3" ] || { echo "Offline bundle is missing runtime/bin/python3" >&2; exit 1; }
verify="$package_dir/app/packaging/verify_bundle.py"
[ -f "$verify" ] || { echo "Offline bundle is missing its manifest verifier" >&2; exit 1; }
"$package_dir/runtime/bin/python3" -B "$verify" --root "$package_dir" >/dev/null

# Stop an existing instance before moving its live code tree. A first install
# has no old configuration and simply continues.
old_env="$data_dir/config/sjfx.env"
# Apply the Linux delivery default only to a genuinely new instance. Existing
# configuration, receipts, or populated state retain their current behavior.
fresh_instance=0
if [ ! -e "$old_env" ] && [ ! -L "$old_env" ] && \
        [ ! -e "$data_dir/install-receipt.json" ] && [ ! -L "$data_dir/install-receipt.json" ] && \
        [ ! -e "$data_dir/config/install-root.txt" ] && [ ! -L "$data_dir/config/install-root.txt" ]; then
    if [ ! -e "$data_dir/state" ] && [ ! -L "$data_dir/state" ]; then
        fresh_instance=1
    elif [ -d "$data_dir/state" ] && [ ! -L "$data_dir/state" ]; then
        # A failed enumeration can also produce no output. Preserve existing
        # behavior unless emptiness was positively established.
        if state_entry=$(find "$data_dir/state" -mindepth 1 -maxdepth 1 -print -quit); then
            [ -n "$state_entry" ] || fresh_instance=1
        fi
    fi
fi
old_install_root=''
[ -f "$data_dir/config/install-root.txt" ] && old_install_root=$(cat "$data_dir/config/install-root.txt" 2>/dev/null || true)
old_stop_root="$install_dir"
if [ -n "$old_install_root" ]; then
    old_stop_root=$(guard_path "$old_install_root")
    case "$old_stop_root" in
        "$data_dir"|"$data_dir/"*)
            echo "Recorded previous install path overlaps install or data path: $old_stop_root" >&2
            exit 1
            ;;
    esac
    case "$data_dir" in
        "$old_stop_root"|"$old_stop_root/"*)
            echo "Recorded previous install path overlaps install or data path: $old_stop_root" >&2
            exit 1
            ;;
    esac
    if [ "$old_stop_root" != "$install_dir" ]; then
        case "$old_stop_root" in
            "$install_dir"|"$install_dir/"*)
                echo "Recorded previous install path overlaps install or data path: $old_stop_root" >&2
                exit 1
                ;;
        esac
        case "$install_dir" in
            "$old_stop_root"|"$old_stop_root/"*)
                echo "Recorded previous install path overlaps install or data path: $old_stop_root" >&2
                exit 1
                ;;
        esac
    fi
fi
old_python="$old_stop_root/runtime/bin/python3"
old_ctl="$old_stop_root/app/sjfxctl.py"
model_settings_path=$("$package_dir/runtime/bin/python3" -B - "$old_env" "$old_stop_root" "$data_dir/state" <<'PY'
import pathlib, sys
env_file, old_root, default_state = map(pathlib.Path, sys.argv[1:])
state = default_state
if env_file.is_file():
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == "SJFX_STATE_DIR":
                raw = value.strip().strip('"').strip("'")
                if raw:
                    state = pathlib.Path(raw).expanduser()
                    if not state.is_absolute():
                        state = old_root / "app" / state
                break
print(state.resolve() / "model_settings.json")
PY
)
if [ -f "$old_env" ] && [ -x "$old_python" ] && [ -f "$old_ctl" ]; then
    if ! "$old_python" -B "$old_ctl" --env-file "$old_env" stop --timeout 30; then
        echo "The existing SJFX instance did not stop cleanly; refusing to replace $install_dir" >&2
        exit 1
    fi
fi

mkdir -p "$data_dir/config" "$data_dir/state" "$data_dir/outputs" \
    "$data_dir/parse_temp" "$data_dir/logs" "$data_dir/cache/huggingface" \
    "$data_dir/outputs/_uploads"
parent=$(dirname -- "$install_dir")
mkdir -p "$parent"
stage=$(mktemp -d "$parent/.sjfx-stage-XXXXXX")
backup=''
metadata_backup=''
clam_database="$data_dir/models/protection/clamav"
clam_stage=''
clam_database_initializing=0
new_install_active=0
linux_policy_initializing=0
linux_policy_profile='preserved'
linux_policy_dir="$data_dir/state/file_protection"
linux_policy_dir_absent=0
[ -e "$linux_policy_dir" ] || [ -L "$linux_policy_dir" ] || linux_policy_dir_absent=1
cleanup_staging() {
    rm -rf -- "$stage" 2>/dev/null || true
    if [ -n "$metadata_backup" ]; then
        rm -rf -- "$metadata_backup" 2>/dev/null || true
    fi
    if [ -n "$clam_stage" ]; then
        rm -rf -- "$clam_stage" 2>/dev/null || true
    fi
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
metadata_backup=$(mktemp -d "$data_dir/.sjfx-metadata-XXXXXX")
snapshot_file() {
    src=$1
    name=$2
    if [ -e "$src" ]; then
        cp -a -- "$src" "$metadata_backup/$name"
    else
        : > "$metadata_backup/$name.absent"
    fi
}
restore_metadata() {
    target=$1
    name=$2
    if [ -e "$metadata_backup/$name.absent" ]; then
        rm -f -- "$target"
    elif [ -e "$metadata_backup/$name" ]; then
        cp -a -- "$metadata_backup/$name" "$target"
    fi
}
make_removable() {
    target=$1
    [ -e "$target" ] || return 0
    find "$target" -type d -exec chmod u+rwx {} + 2>/dev/null || true
    find "$target" -type f -exec chmod u+rw {} + 2>/dev/null || true
}
snapshot_file "$data_dir/config/sjfx.env" env
snapshot_file "$data_dir/config/install-root.txt" install-root
snapshot_file "$data_dir/install-receipt.json" install-receipt
snapshot_file "$model_settings_path" model-settings
snapshot_file "$linux_policy_dir/package-policy.json" linux-policy
snapshot_file "$linux_policy_dir/package-policy-history.jsonl" linux-policy-history
rollback() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ]; then
        # A signal may arrive after mv succeeds but before the state variable
        # is assigned. The vanished stage also proves activation occurred.
        if [ "$new_install_active" -ne 0 ] || { [ ! -e "$stage" ] && [ -e "$install_dir" ]; }; then
            make_removable "$install_dir"
            rm -rf -- "$install_dir" 2>/dev/null || true
        fi
        if [ -n "$backup" ] && [ -e "$backup" ] && [ ! -e "$install_dir" ]; then
            mv "$backup" "$install_dir" 2>/dev/null || true
        fi
        restore_metadata "$data_dir/config/sjfx.env" env || true
        restore_metadata "$data_dir/config/install-root.txt" install-root || true
        restore_metadata "$data_dir/install-receipt.json" install-receipt || true
        restore_metadata "$model_settings_path" model-settings || true
        if [ "$linux_policy_initializing" -ne 0 ]; then
            restore_metadata "$linux_policy_dir/package-policy.json" linux-policy || true
            restore_metadata "$linux_policy_dir/package-policy-history.jsonl" linux-policy-history || true
            if [ "$linux_policy_dir_absent" -ne 0 ]; then
                rmdir -- "$linux_policy_dir" 2>/dev/null || true
            fi
        fi
        if [ "$clam_database_initializing" -ne 0 ] && [ -n "$clam_stage" ] && [ ! -e "$clam_stage" ]; then
            make_removable "$clam_database"
            rm -rf -- "$clam_database" 2>/dev/null || true
        fi
        echo "SJFX activation failed; previous install restored when possible" >&2
    fi
    rm -rf -- "$metadata_backup" 2>/dev/null || true
    rm -rf -- "$stage" 2>/dev/null || true
    if [ -n "$clam_stage" ]; then
        rm -rf -- "$clam_stage" 2>/dev/null || true
    fi
    exit "$status"
}
trap rollback EXIT
# Keep signatures in instance state so an offline database update survives
# application replacement. Never overwrite a pre-existing instance database.
if [ -d "$package_dir/models/protection/clamav" ]; then
    [ "$(realpath -m -- "$clam_database")" = "$clam_database" ] || {
        echo "The instance ClamAV database path must not contain symbolic links" >&2; exit 1;
    }
    if [ ! -e "$clam_database" ]; then
        clam_parent=$(dirname -- "$clam_database")
        mkdir -p "$clam_parent"
        clam_stage=$(mktemp -d "$clam_parent/.sjfx-clam-XXXXXX")
        cp -a "$package_dir/models/protection/clamav/." "$clam_stage/"
        make_removable "$clam_stage"
        clam_database_initializing=1
        mv -T "$clam_stage" "$clam_database"
    elif [ ! -d "$clam_database" ]; then
        echo "The instance ClamAV database path is not a directory: $clam_database" >&2; exit 1
    fi
fi
cp -a "$package_dir/app" "$package_dir/runtime" "$package_dir/models" "$package_dir/tools" "$package_dir/config" "$stage/"
cp -a "$package_dir/manifest.json" "$stage/manifest.json"
[ -f "$package_dir/INSTALL.md" ] && cp -a "$package_dir/INSTALL.md" "$stage/INSTALL.md" || true
for name in install_windows.ps1 install_linux.sh upgrade_windows.ps1 upgrade_linux.sh uninstall_windows.ps1 uninstall_linux.sh; do
    [ -f "$package_dir/$name" ] && cp -a "$package_dir/$name" "$stage/$name" || true
done
[ -f "$package_dir/start_sjfx.sh" ] && cp -a "$package_dir/start_sjfx.sh" "$stage/start_sjfx.sh" || true
if [ -e "$install_dir" ]; then
    backup="$install_dir.backup-$(date -u +%Y%m%d-%H%M%S)-$$"
    [ ! -e "$backup" ] || { echo "Backup path already exists: $backup" >&2; backup=''; exit 1; }
    mv "$install_dir" "$backup"
fi
mv "$stage" "$install_dir"
new_install_active=1
# Application inputs are immutable; all mutable paths live below $data_dir.
find "$install_dir" -type f -exec chmod a-w {} +
find "$install_dir" -type d -exec chmod a-w {} +

env_file="$data_dir/config/sjfx.env"
created_env=0
if [ ! -f "$env_file" ]; then
    "$install_dir/runtime/bin/python3" -B - "$install_dir/config/sjfx.env.template" "$env_file" "$data_dir" "$install_dir" <<'PY'
import pathlib, secrets, sys
template, target, data_dir, install_dir = map(pathlib.Path, sys.argv[1:])
text = template.read_text(encoding="utf-8")
text = text.replace("__DATA_DIR__", str(data_dir)).replace("__INSTALL_DIR__", str(install_dir))
text += "SJFX_API_ACCESS_TOKEN=" + secrets.token_urlsafe(48) + "\n"
target.write_text(text, encoding="utf-8")
target.chmod(0o600)
PY
    created_env=1
elif [ -n "$old_install_root" ] && [ "$old_install_root" != "$install_dir" ]; then
    "$install_dir/runtime/bin/python3" -B - "$env_file" "$old_stop_root" "$install_dir" "$model_settings_path" <<'PY'
import json, pathlib, sys
target, old_root, new_root, model_settings = map(pathlib.Path, sys.argv[1:])
keys = {
    "SJFX_MODELS_DIR", "MODELS_DIR", "SJFX_TOOLS_DIR", "TOOLS_DIR",
    "SJFX_DOCLING_ARTIFACTS_DIR", "DOCLING_ARTIFACTS_DIR", "DOCLING_CACHE_DIR",
    "SJFX_RAPIDOCR_MODEL_DIR", "RAPIDOCR_MODEL_DIR", "SJFX_EMBED_MODEL_PATH",
    "VLLM_EMBED_MODEL_PATH", "SJFX_EMBED_TOKENIZER_PATH", "LLM_TOKENIZER_PATH",
    "SJFX_MANAGED_EMBED_MODEL_PATH",
    "CONVERSATION_TOKENIZER_PATH", "TRANSLATION_QUICKMT_MODEL_DIR",
    "TRANSLATION_MODEL_PATH", "TRANSLATION_CT2_MODEL_PATH", "VLLM_MODEL_PATH",
    "SJFX_DESKTOP_HELPER_BINARY", "SJFX_PROTECTION_CLAM",
    "SJFX_PROTECTION_CLAM_DATABASE", "SJFX_PROTECTION_CLAM_CERTS",
    "SJFX_PROTECTION_YARA", "SJFX_PROTECTION_YARA_RULES",
    "SJFX_PROTECTION_RSPAMD", "SJFX_MSG_PYTHON",
}
def rebase(value):
    if value == str(old_root) or value.startswith(str(old_root) + "/"):
        return str(new_root) + value[len(str(old_root)):]
    return value
lines = []
for line in target.read_text(encoding="utf-8-sig").splitlines(keepends=True):
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        if key.strip() in keys:
            ending = "\n" if value.endswith("\n") else ""
            raw = (value[:-1] if ending else value).strip()
            quote = raw[:1] if raw[:1] in "\"'" else ""
            tail = raw[-1:] if raw[-1:] in "\"'" and raw[-1:] == quote else ""
            inner = raw[len(quote):len(raw)-len(tail) if tail else None]
            updated = rebase(inner)
            if updated != inner:
                inner = updated
                value = quote + inner + tail + ending
                line = key + "=" + value
    lines.append(line)
target.write_text("".join(lines), encoding="utf-8")
if model_settings.is_file():
    settings = json.loads(model_settings.read_text(encoding="utf-8"))
    overrides = settings.get("env_overrides", {})
    changed = False
    if not isinstance(overrides, dict):
        raise RuntimeError("Persisted model settings have invalid overrides")
    for key in ("SJFX_EMBED_TOKENIZER_PATH", "LLM_TOKENIZER_PATH", "CONVERSATION_TOKENIZER_PATH"):
        if key in overrides and isinstance(overrides[key], str):
            updated = rebase(overrides[key])
            if updated != overrides[key]:
                overrides[key] = updated
                changed = True
    if changed:
        model_settings.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi
# Fill missing local runtime and reviewed business defaults on upgrade.
# Explicit instance values and generation/embedding selections take priority.
"$install_dir/runtime/bin/python3" -B - "$env_file" "$install_dir/config/sjfx.env.template" "$data_dir" "$install_dir" <<'PY'
import pathlib, sys
target, template, data_dir, install_dir = map(pathlib.Path, sys.argv[1:])
keys = {
    "SJFX_LOCAL_AGENT_ENDPOINT", "SJFX_DESKTOP_ACCESS_AUDIT", "SJFX_DESKTOP_HELPER_BINARY",
    "SJFX_PROTECTION_CLAM", "SJFX_PROTECTION_CLAM_DATABASE", "SJFX_PROTECTION_CLAM_CERTS",
    "SJFX_PROTECTION_SCAN_WORKDIR", "SJFX_MANAGED_EMBEDDING", "SJFX_MANAGED_EMBED_PORT",
    "SJFX_MANAGED_EMBED_MODEL_PATH",
    "SJFX_OFFLINE_INSTALLATION",
}
text = target.read_text(encoding="utf-8-sig")
present = {line.split("=", 1)[0].strip() for line in text.splitlines()
           if "=" in line and not line.lstrip().startswith("#")}
missing = []
in_business_defaults = False
for line in template.read_text(encoding="utf-8").splitlines():
    if line.strip() == '# BEGIN SJFX LINUX BUSINESS DEFAULTS':
        in_business_defaults = True
        continue
    if line.strip() == '# END SJFX LINUX BUSINESS DEFAULTS':
        in_business_defaults = False
        continue
    if "=" not in line or line.lstrip().startswith("#"):
        continue
    key = line.split("=", 1)[0].strip()
    if (key in keys or in_business_defaults) and key not in present:
        missing.append(line.replace("__DATA_DIR__", str(data_dir)).replace("__INSTALL_DIR__", str(install_dir)))
        present.add(key)
if missing:
    target.write_text(text + ("" if text.endswith("\n") or not text else "\n")
                      + "\n".join(missing) + "\n", encoding="utf-8")
PY
# Persist the supported switch before the first launcher start. The shared
# desktop launcher preserves an existing policy; Windows defaults are untouched.
if [ "$fresh_instance" -eq 1 ] && [ "$created_env" -eq 1 ]; then
    linux_policy_initializing=1
    linux_policy_profile=$("$install_dir/runtime/bin/python3" -B - "$install_dir/app" "$linux_policy_dir" <<'PY'
import pathlib, sys
app, core = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(app))
from sjfx_protection.policy import policy_path, write_policy
policy = policy_path(core)
history = core / "package-policy-history.jsonl"
if core.is_symlink() or policy.exists() or policy.is_symlink() or history.exists() or history.is_symlink():
    print("preserved")
else:
    write_policy(core, "linux-installer", True, {"enabled": False},
                 "Fresh Linux installation: protection disabled by delivery default")
    print("disabled")
PY
)
    if [ "$linux_policy_profile" = 'preserved' ]; then
        linux_policy_initializing=0
    fi
fi
printf '%s' "$install_dir" > "$data_dir/config/install-root.txt"
chmod 700 "$data_dir" "$data_dir/config" "$data_dir/state" "$data_dir/outputs" "$data_dir/parse_temp" "$data_dir/logs" || true
"$install_dir/runtime/bin/python3" -B - "$data_dir/install-receipt.json" "$install_dir" "$data_dir" "$backup" <<'PY'
import json, pathlib, sys
target, install_dir, data_dir = map(pathlib.Path, sys.argv[1:4])
backup = sys.argv[4]
target.write_text(json.dumps({"install_dir": str(install_dir), "data_dir": str(data_dir), "backup_dir": backup or None}, indent=2) + "\n", encoding="utf-8")
target.chmod(0o600)
PY
echo "SJFX installed. Configuration: $env_file"
if [ "$linux_policy_profile" = 'disabled' ]; then
    echo "File protection is disabled by default for this new Linux instance; malware admission and protection approvals are inactive."
    echo "Login, owner isolation, and configured scan path restrictions remain enabled. An administrator can enable file protection in its settings."
else
    echo "Existing instance configuration and file protection policy have been preserved."
fi
echo "For a new instance, initialize the administrator: \"$install_dir/runtime/bin/python3\" -B -X utf8 \"$install_dir/app/sjfxctl.py\" --env-file \"$env_file\" init-admin"
echo "Start with: $install_dir/start_sjfx.sh --env-file $env_file"
rm -rf -- "$metadata_backup" 2>/dev/null || true
trap - EXIT HUP INT TERM
