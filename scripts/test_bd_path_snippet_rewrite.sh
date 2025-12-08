#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/install.sh"

# shellcheck disable=SC1090
source "${INSTALL_SCRIPT}"

COL_PASS="\033[1;32m"
COL_FAIL="\033[1;31m"
COL_INFO="\033[1;36m"
COL_RESET="\033[0m"

log_info() {
  printf "%b[TEST]%b %s\n" "${COL_INFO}" "${COL_RESET}" "$*"
}

log_pass() {
  printf "%b[PASS]%b %s\n" "${COL_PASS}" "${COL_RESET}" "$*"
}

log_fail() {
  printf "%b[FAIL]%b %s\n" "${COL_FAIL}" "${COL_RESET}" "$*" >&2
}

expected_block() {
  local dir="$1"
  cat <<EOF
# >>> MCP Agent Mail bd path ${dir}
if [[ ":\$PATH:" != *":${dir}:"* ]]; then
  export PATH="${dir}:\$PATH"
fi
# <<< MCP Agent Mail bd path
EOF
}

extract_block() {
  local file="$1"
  local marker="$2"
  local end_marker="$3"

  awk -v start="${marker}" -v end="${end_marker}" '
    $0 == start { print; in_block = 1; next }
    in_block && $0 == end { print; exit }
    in_block { print }
  ' "${file}"
}

run_case() {
  local name="$1"
  local seed_content="$2"
  local dir="$3"

  log_info "Running case: ${name}"
  local tmp
  tmp=$(mktemp)
  printf '%s\n' "${seed_content}" > "${tmp}"

  if ! append_path_snippet "${dir}" "${tmp}"; then
    log_fail "${name}: append_path_snippet returned non-zero"
    rm -f "${tmp}"
    return 1
  fi

  local marker="# >>> MCP Agent Mail bd path ${dir}"
  local end_marker="# <<< MCP Agent Mail bd path"
  local actual_block
  actual_block=$(extract_block "${tmp}" "${marker}" "${end_marker}" || true)
  local expected
  expected=$(expected_block "${dir}")

  if [[ "${actual_block}" != "${expected}" ]]; then
    log_fail "${name}: snippet mismatch"
    printf "----- actual -----\n%s\n----- expected -----\n%s\n" "${actual_block}" "${expected}" >&2
    rm -f "${tmp}"
    return 1
  fi

  log_pass "${name}"
  rm -f "${tmp}"
  return 0
}

main() {
  local failures=0
  local dir="/home/example/bin"

  run_case "Rewrite legacy \$PATH block" \
"# >>> MCP Agent Mail bd path ${dir}
if [[ \":\\\$PATH:\" != *\":${dir}:\"* ]]; then
  export PATH=\"${dir}:\\\$PATH\"
fi
# <<< MCP Agent Mail bd path" \
    "${dir}" || failures=$((failures + 1))

  run_case "Insert snippet into empty file" "" "${dir}" || failures=$((failures + 1))

  if [[ "${failures}" -gt 0 ]]; then
    log_fail "${failures} case(s) failed"
    exit 1
  fi

  log_pass "All cases succeeded"
}

main "$@"

