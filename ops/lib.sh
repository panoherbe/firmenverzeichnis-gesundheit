# shellcheck shell=bash
# Shared helpers for ops/*.sh (sourced, not executed). Expects ROOT to be set to the repo root.

load_env() {  # KEY=VALUE lines from $ROOT/.env, without overriding the environment and without eval
  local f="$ROOT/.env" line k v
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k=${line%%=*}; v=${line#*=}
    [[ $k =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
    if [ -z "${!k+x}" ]; then export "$k=$v"; fi
  done < "$f"
  return 0
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
filesize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }

log_json() {  # file key=value ... ; numbers/true/false/null unquoted, everything else as JSON string
  local f=$1 kv k v out; shift
  out="{\"at\":\"$(now_iso)\",\"host\":\"$(hostname -s)\""
  for kv in "$@"; do
    k=${kv%%=*}; v=${kv#*=}
    if [[ $v =~ ^(true|false|null|-?[0-9]+(\.[0-9]+)?)$ ]]; then out+=",\"$k\":$v"; else
      v=${v//\\/\\\\}; v=${v//\"/\\\"}; out+=",\"$k\":\"$v\""; fi
  done
  echo "$out}" >> "$f"
}

db_url_for() {  # DATABASE_URL with the database name replaced: db_url_for postgres
  echo "${DATABASE_URL%/*}/$1"
}
