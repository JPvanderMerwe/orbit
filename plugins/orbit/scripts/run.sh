#!/bin/sh
# Runs one of Orbit's Python scripts with whichever Python this computer has:
#   sh run.sh hook.py [args...]
# macOS and Linux use python3. On Windows, Claude Code runs commands in Git Bash, where python3 is
# often missing, so the Python launcher (py) or python is used instead.
script="${0%[/\\]*}/$1"
shift
case "${OSTYPE:-}" in
  msys* | cygwin* | win*)
    if command -v py >/dev/null 2>&1; then exec py -3 "$script" "$@"; fi
    if command -v python >/dev/null 2>&1; then exec python "$script" "$@"; fi
    ;;
esac
exec python3 "$script" "$@"
