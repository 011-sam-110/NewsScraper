#!/usr/bin/env bash
set -euo pipefail

: "${HOME:?HOME must be set}"
config_dir="$HOME/.config/provenance-incident"
mkdir -p "$config_dir"
chmod 700 "$config_dir"

if [[ "${PROVENANCE_INCIDENT_FAKE:-0}" == "1" ]]; then
  default_url="https://example.supabase.co"
  default_service_key="fake-service-key-for-tests-only-0123456789"
  default_deepseek_key="fake-deepseek-key-for-tests-only-0123456789"
else
  default_url=""
  default_service_key=""
  default_deepseek_key=""
fi

read -r -p "Supabase URL [$default_url]: " supabase_url
supabase_url="${supabase_url:-$default_url}"
read -r -s -p "Supabase service role key: " service_key
service_key="${service_key:-$default_service_key}"
printf '\n'
read -r -s -p "DeepSeek key for Ukraine: " deepseek_key
deepseek_key="${deepseek_key:-$default_deepseek_key}"
deepseek_key="${deepseek_key%$'\r'}"
printf '\n'

if [[ "$supabase_url" != https://* ]]; then
  printf 'SUPABASE_URL must start with https://\n' >&2
  exit 2
fi
if (( ${#service_key} < 32 )); then
  printf 'SUPABASE_SECRET_KEY must contain at least 32 characters\n' >&2
  exit 2
fi
if [[ -n "$deepseek_key" ]] && (( ${#deepseek_key} < 32 )); then
  printf 'DEEPSEEK_KEY_UKRAINE must contain at least 32 characters\n' >&2
  exit 2
fi

umask 077
cat > "$config_dir/ukraine.env" <<EOF
NEXT_PUBLIC_SUPABASE_URL=$supabase_url
SUPABASE_SECRET_KEY=$service_key
DEEPSEEK_KEY_UKRAINE=$deepseek_key
EOF
if [[ "${OS:-}" == "Windows_NT" || "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  current_user="${USERNAME:-$(id -un)}"
  icacls "$config_dir/ukraine.env" /inheritance:r /grant:r "$current_user:F" >/dev/null
else
  chmod 600 "$config_dir/ukraine.env"
fi
printf 'Saved %s with a current-user-only ACL. No values shown.\n' "$config_dir/ukraine.env"
