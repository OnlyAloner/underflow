#!/bin/sh
# Ставит скилл underflow в Claude Code и клиент uf на PATH.
#   ./install.sh                         # сервер на 127.0.0.1:8800
#   ./install.sh --url https://uf.example.com --token XXX --post auto
set -eu

here=$(cd "$(dirname "$0")" && pwd)
url="" token="" post=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) url="$2"; shift 2 ;;
    --token) token="$2"; shift 2 ;;
    --post) post="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

skills="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
dest="$skills/underflow"
mkdir -p "$dest" "$HOME/.local/bin"
cp "$here/skill/underflow/SKILL.md" "$dest/SKILL.md"
cp "$here/client/uf" "$dest/uf"
chmod +x "$dest/uf"
ln -sf "$dest/uf" "$HOME/.local/bin/uf"

conf="${XDG_CONFIG_HOME:-$HOME/.config}/underflow"
mkdir -p "$conf"
touch "$conf/env"
chmod 600 "$conf/env"
set_kv() {  # заменить или дописать KEY=VALUE
  grep -v "^$1=" "$conf/env" > "$conf/env.tmp" || true
  echo "$1=$2" >> "$conf/env.tmp"
  mv "$conf/env.tmp" "$conf/env"
}
[ -n "$url" ] && set_kv UNDERFLOW_URL "$url"
[ -n "$token" ] && set_kv UNDERFLOW_TOKEN "$token"
[ -n "$post" ] && set_kv UNDERFLOW_POST "$post"
grep -q '^UNDERFLOW_POST=' "$conf/env" || set_kv UNDERFLOW_POST ask

echo "skill:  $dest/SKILL.md"
echo "client: $HOME/.local/bin/uf"
echo "config: $conf/env"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to PATH, or the skill will call $dest/uf" ;; esac
"$dest/uf" stats >/dev/null 2>&1 && echo "server: reachable" || echo "server: not reachable yet (start one: docker compose up -d)"
