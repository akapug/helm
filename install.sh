#!/bin/sh
# helm install — run from the checkout root: ./install.sh
#
# helm is Python standard library only. There is nothing to compile, nothing
# to pip install, and no state written outside $HELM_HOME (default ~/.helm).
# This script only (1) checks your python3 is new enough, (2) offers `helm`
# on your PATH via one symlink, and (3) runs the self-check so you start from
# a known-good state instead of a hopeful one.
set -e

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
helm_bin="$here/bin/helm"

if [ ! -x "$helm_bin" ]; then
  echo "install: run this from the helm checkout root — bin/helm not found" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "install: python3 not found on PATH — helm needs Python 3.9+" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
then
  echo "install: python3 is $(python3 -V 2>&1) — helm needs Python 3.9+" >&2
  exit 1
fi

link_dir="${HELM_INSTALL_BIN:-$HOME/.local/bin}"
link="$link_dir/helm"
if [ -e "$link" ] && [ "$(readlink "$link" 2>/dev/null)" != "$helm_bin" ]; then
  echo "install: $link exists and is not this checkout — leaving it alone."
  echo "install: run \"$helm_bin\" directly, or remove that link and rerun."
else
  mkdir -p "$link_dir"
  ln -sf "$helm_bin" "$link"
  echo "install: helm -> $link"
  case ":$PATH:" in
    *":$link_dir:"*) ;;
    *) echo "install: NOTE — $link_dir is not on your PATH" ;;
  esac
fi

echo "install: $("$helm_bin" --version)"
echo "install: running the self-check (helm doctor)..."
"$helm_bin" doctor || {
  echo "install: doctor reported problems above — helm still works from the" >&2
  echo "install: checkout; fix what it names, then rerun ./install.sh" >&2
  exit 1
}
echo "install: done. Next: docs/NEW_AGENT_GUIDE.md (agents) or README.md."
