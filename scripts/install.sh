#!/bin/sh
# helm install — put `helm` on your PATH. That is the whole job.
#
# helm is stdlib-only Python and `bin/helm` resolves the package through its own
# symlink, so there is nothing to build, vendor, compile or virtualenv: the
# install IS a symlink. This script exists to do that one thing carefully —
# refuse to clobber a file it did not create, tell you when the target is not on
# your PATH, and verify the result by actually RUNNING the installed binary
# rather than assuming the link works.
#
#   sh scripts/install.sh              # -> ~/.local/bin/helm
#   sh scripts/install.sh --dry-run    # say what would happen, change nothing
#   sh scripts/install.sh --prefix DIR # somewhere else
#   sh scripts/install.sh --uninstall  # remove a link THIS script created
#
# POSIX sh, no bashisms, no dependencies — a release whose installer needs its
# own toolchain is not a zero-dependency release.
set -eu

MIN_MAJOR=3
MIN_MINOR=9

PREFIX="${HELM_INSTALL_DIR:-$HOME/.local/bin}"
DRY=0
FORCE=0
UNINSTALL=0

die() { echo "helm install: $1" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --prefix) shift; [ $# -gt 0 ] || die "--prefix wants a directory"; PREFIX="$1" ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option '$1' (--help)" ;;
  esac
  shift
done

# Resolve this script's directory THROUGH symlinks, so the installer works when
# it is itself reached via a link. `cd -P` is the portable readlink -f.
SCRIPT_DIR=$(cd -P "$(dirname "$0")" && pwd)
REPO=$(cd -P "$SCRIPT_DIR/.." && pwd)
SRC="$REPO/bin/helm"
DEST="$PREFIX/helm"

[ -f "$SRC" ] || die "cannot find $SRC — run this from a helm checkout"

if [ "$UNINSTALL" = 1 ]; then
  if [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; then
    echo "helm install: nothing at $DEST — already uninstalled"; exit 0
  fi
  # Only remove what we would have created. Deleting a real file someone else
  # put there would be a destructive surprise, so it is refused, not forced.
  if [ -L "$DEST" ]; then
    [ "$DRY" = 1 ] && { echo "would remove symlink $DEST"; exit 0; }
    rm "$DEST"; echo "helm install: removed $DEST"; exit 0
  fi
  die "$DEST is a regular file, not a symlink this script created — remove it yourself"
fi

# --- python floor -----------------------------------------------------------
# Checked BEFORE linking: a helm on PATH that cannot start is worse than no
# helm, because the failure surfaces later and somewhere else.
PY=$(command -v python3 2>/dev/null || true)
[ -n "$PY" ] || die "python3 not found on PATH — helm needs Python ${MIN_MAJOR}.${MIN_MINOR}+"
PY_OK=$("$PY" -c 'import sys; print(1 if sys.version_info[:2] >= ('"$MIN_MAJOR"', '"$MIN_MINOR"') else 0)')
PY_VER=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
[ "$PY_OK" = 1 ] || die "python3 is $PY_VER — helm needs ${MIN_MAJOR}.${MIN_MINOR}+"
echo "helm install: python3 $PY_VER at $PY (ok)"

# --- the link ---------------------------------------------------------------
if [ -L "$DEST" ]; then
  CUR=$(cd -P "$(dirname "$DEST")" && cd -P "$(dirname "$(readlink "$DEST")")" 2>/dev/null && pwd)/$(basename "$(readlink "$DEST")") || CUR=""
  if [ "$CUR" = "$SRC" ]; then
    echo "helm install: already linked $DEST -> $SRC (nothing to do)"
    "$DEST" --version 2>/dev/null || die "the existing link does not run"
    exit 0
  fi
  [ "$FORCE" = 1 ] || die "$DEST already links elsewhere ($(readlink "$DEST")) — --force to repoint"
elif [ -e "$DEST" ]; then
  [ "$FORCE" = 1 ] || die "$DEST exists and is not a symlink — --force to replace"
fi

if [ "$DRY" = 1 ]; then
  echo "would mkdir -p $PREFIX"
  echo "would link    $DEST -> $SRC"
  echo "would verify  $DEST --version"
  exit 0
fi

mkdir -p "$PREFIX"
ln -sf "$SRC" "$DEST"
echo "helm install: linked $DEST -> $SRC"

# --- verify by RUNNING it ---------------------------------------------------
# The install is not "done" because a link exists; it is done when the thing on
# your PATH answers.
VER=$("$DEST" --version 2>&1) || die "installed but will not run: $VER"
echo "helm install: $VER"

# --- PATH honesty -----------------------------------------------------------
case ":$PATH:" in
  *":$PREFIX:"*) echo "helm install: $PREFIX is on your PATH — run: helm doctor" ;;
  *) echo "helm install: NOTE $PREFIX is not on your PATH. Add it:"
     echo "    export PATH=\"$PREFIX:\$PATH\""
     echo "  until then, run it by full path: $DEST doctor" ;;
esac
