#!/usr/bin/env sh
set -eu

INSTALL_ROOT="${COMIC_SOL_INSTALL_ROOT:-$HOME/.local/share/comic-sol}"
ARCHIVE=""
SHA256=""
URL=""
UNINSTALL=0
INSTALL_LOCK_DIR=""
LOCK_HELD=0
INSTALL_MARKER_NAME=".comic-sol-install"
INSTALL_MARKER_MAGIC="comic-sol-install-v1"

acquire_install_lock() {
  lock_parent=$(dirname "$INSTALL_LOCK_DIR")
  mkdir -p -- "$lock_parent"
  if ! mkdir -- "$INSTALL_LOCK_DIR" 2>/dev/null; then
    owner=$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)
    case "$owner" in
      '')
        echo "another Comic Sol installer is using this install root" >&2
        return 1
        ;;
      *[!0-9]*)
        echo "cannot validate existing install lock" >&2
        return 1
        ;;
      *)
        if kill -0 "$owner" 2>/dev/null; then
          echo "another Comic Sol installer is using this install root" >&2
          return 1
        fi
        tombstone="${INSTALL_LOCK_DIR}.stale.$$"
        if ! mv -- "$INSTALL_LOCK_DIR" "$tombstone" 2>/dev/null; then
          echo "another Comic Sol installer is using this install root" >&2
          return 1
        fi
        if ! mkdir -- "$INSTALL_LOCK_DIR" 2>/dev/null; then
          rm -rf -- "$tombstone"
          echo "another Comic Sol installer is using this install root" >&2
          return 1
        fi
        rm -rf -- "$tombstone"
        ;;
    esac
  fi
  LOCK_HELD=1
  if ! printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/pid"; then
    rm -rf -- "$INSTALL_LOCK_DIR"
    LOCK_HELD=0
    return 1
  fi
}

release_install_lock() {
  [ "$LOCK_HELD" -eq 1 ] || return 0
  rm -rf -- "$INSTALL_LOCK_DIR"
  LOCK_HELD=0
}

canonical_install_root() {
  (cd -P -- "$1" && pwd -P)
}

cleanup_install() {
  rollback
  release_install_lock
  rm -rf -- "$TMP"
}

abort_uninstall() {
  release_install_lock
  exit 130
}

abort_install() {
  cleanup_install
  exit 130
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --archive) ARCHIVE=$2; shift 2 ;;
    --sha256) SHA256=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --install-root) INSTALL_ROOT=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  if [ ! -e "$INSTALL_ROOT" ]; then
    echo "Comic Sol runtime is already removed. User projects were preserved."
    exit 0
  fi
  if [ ! -d "$INSTALL_ROOT" ]; then
    echo "refusing to uninstall: install root is not a directory" >&2
    exit 1
  fi
else
  if [ -z "$SHA256" ]; then
    echo "--sha256 is required for this unsigned prerelease" >&2
    exit 2
  fi
  mkdir -p "$INSTALL_ROOT"
fi

INSTALL_ROOT=$(canonical_install_root "$INSTALL_ROOT")
INSTALL_LOCK_DIR="${INSTALL_ROOT}.lock"
if [ "$UNINSTALL" -eq 1 ]; then
  acquire_install_lock
  trap 'release_install_lock' EXIT
  trap 'abort_uninstall' INT TERM

  CURRENT_ROOT=$(pwd -P)
  HOME_ROOT=$(cd -P -- "$HOME" && pwd -P)
  case "$INSTALL_ROOT" in
    /) echo "refusing to uninstall from a filesystem root" >&2; exit 1 ;;
  esac
  if [ "$INSTALL_ROOT" = "$HOME_ROOT" ] || [ "$INSTALL_ROOT" = "$CURRENT_ROOT" ]; then
    echo "refusing to uninstall from a sensitive directory" >&2
    exit 1
  fi
  if [ -e "$INSTALL_ROOT/.git" ] || [ -e "$INSTALL_ROOT/project.json" ]; then
    echo "refusing to uninstall from a repository or Comic Sol project root" >&2
    exit 1
  fi

  INSTALL_MARKER="$INSTALL_ROOT/$INSTALL_MARKER_NAME"
  ACTIVE_VERSION_FILE="$INSTALL_ROOT/active-version"
  if [ ! -f "$INSTALL_MARKER" ] || [ ! -f "$ACTIVE_VERSION_FILE" ]; then
    echo "refusing to uninstall: install root is not a registered Comic Sol runtime; reinstall or upgrade this root first" >&2
    exit 1
  fi
  MARKER_LINE_COUNT=$(awk 'END { print NR }' "$INSTALL_MARKER")
  MARKER_MAGIC=$(sed -n '1p' "$INSTALL_MARKER")
  MARKER_VERSION=$(sed -n '2p' "$INSTALL_MARKER")
  MARKER_ROOT=$(sed -n '3p' "$INSTALL_MARKER")
  ACTIVE_VERSION=$(sed -n '1p' "$ACTIVE_VERSION_FILE")
  if [ "$MARKER_LINE_COUNT" -ne 3 ] ||
     [ "$MARKER_MAGIC" != "$INSTALL_MARKER_MAGIC" ] ||
     [ -z "$MARKER_VERSION" ] ||
     [ "$MARKER_VERSION" != "$ACTIVE_VERSION" ] ||
     [ "$MARKER_ROOT" != "$INSTALL_ROOT" ]; then
    echo "refusing to uninstall: install registration is invalid; reinstall or upgrade this root first" >&2
    exit 1
  fi

  for child in bin versions .bin.rollback bin.new; do
    rm -rf -- "$INSTALL_ROOT/$child"
  done
  for child in active-version.new .comic-sol-install.new active-version "$INSTALL_MARKER_NAME"; do
    rm -f -- "$INSTALL_ROOT/$child"
  done
  rmdir -- "$INSTALL_ROOT" 2>/dev/null || true
  release_install_lock
  echo "Comic Sol runtime removed. User projects were preserved."
  exit 0
fi

acquire_install_lock
trap 'release_install_lock' EXIT INT TERM
TMP=$(mktemp -d)
INSTALL_STARTED=0
COMMITTED=0
PREVIOUS_VERSION=""
PREVIOUS_POINTER=0
TARGET=""
TARGET_BACKUP=""
STABLE_RUNTIME=""
STABLE_BACKUP=""
STABLE_PUBLISHED=0
TARGET_PUBLISHED=0
ROLLED_BACK=0
rollback() {
  [ "$INSTALL_STARTED" -eq 1 ] || return 0
  [ "$COMMITTED" -eq 1 ] || [ "$ROLLED_BACK" -eq 1 ] && return 0
  ROLLED_BACK=1
  if [ -n "$STABLE_BACKUP" ] && [ -d "$STABLE_BACKUP" ]; then
    rm -rf -- "$STABLE_RUNTIME"
    mv -- "$STABLE_BACKUP" "$STABLE_RUNTIME"
  elif [ "$STABLE_PUBLISHED" -eq 1 ]; then
    rm -rf -- "$STABLE_RUNTIME"
  fi
  if [ -n "$TARGET_BACKUP" ] && [ -d "$TARGET_BACKUP" ]; then
    rm -rf -- "$TARGET"
    mv -- "$TARGET_BACKUP" "$TARGET"
  elif [ "$TARGET_PUBLISHED" -eq 1 ]; then
    rm -rf -- "$TARGET"
  fi
  rm -rf -- "$TARGET.new" "$INSTALL_ROOT/bin.new"
  rm -f -- "$INSTALL_ROOT/.comic-sol-install.new"
  if [ "$PREVIOUS_POINTER" -eq 1 ]; then
    printf '%s\n' "$PREVIOUS_VERSION" > "$INSTALL_ROOT/active-version"
  else
    rm -f -- "$INSTALL_ROOT/active-version"
  fi
}
trap 'cleanup_install' EXIT
trap 'abort_install' INT TERM

if [ -n "$URL" ]; then
  ARCHIVE="$TMP/comic-sol.zip"
  curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2 "$URL" -o "$ARCHIVE"
fi
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "Provide --archive PATH or --url HTTPS_URL" >&2
  exit 2
fi

ACTUAL=$(sha256sum "$ARCHIVE" | cut -d ' ' -f 1 | tr '[:upper:]' '[:lower:]')
EXPECTED=$(printf '%s' "$SHA256" | tr '[:upper:]' '[:lower:]')
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "SHA256 mismatch" >&2
  exit 1
fi

validate_zip() {
  archive=$1
  listing="$TMP/archive-members"
  if ! unzip -Z1 "$archive" > "$listing"; then
    echo "archive member validation failed" >&2
    exit 1
  fi
  if [ ! -s "$listing" ]; then
    echo "archive member validation failed: archive is empty" >&2
    exit 1
  fi
  while IFS= read -r member; do
    case "$member" in
      comic-sol|comic-sol/*) ;;
      *) echo "unsafe archive member: $member" >&2; exit 1 ;;
    esac
    case "$member" in
      ../*|*/../*|./*|*/./*|*//*|/*) echo "unsafe archive member: $member" >&2; exit 1 ;;
      *\\*|[A-Za-z]:/*) echo "unsafe archive member: $member" >&2; exit 1 ;;
    esac
  done < "$listing"
  if ! unzip -Z -l "$archive" | awk 'NR > 3 && $1 ~ /^l/ { found = 1 } END { exit found }'; then
    echo "unsafe archive member: symbolic links are not allowed" >&2
    exit 1
  fi
}

STAGE="$TMP/stage"
mkdir -p "$STAGE"
case "$ARCHIVE" in
  *.zip) validate_zip "$ARCHIVE"; unzip -q -o "$ARCHIVE" -d "$STAGE" ;;
  *) echo "Unsupported archive; POSIX installer currently requires .zip" >&2; exit 2 ;;
esac
RUNTIME="$STAGE/comic-sol"
EXE="$RUNTIME/comic-sol"
[ -d "$RUNTIME" ] || { echo "archive must contain top-level comic-sol runtime" >&2; exit 1; }
[ -f "$EXE" ] || { echo "archive executable is missing" >&2; exit 1; }
chmod 755 "$EXE"
VERSION_OUTPUT=$("$EXE" --version) || { echo "unable to determine a valid runtime version" >&2; exit 1; }
VERSION=$(printf '%s\n' "$VERSION_OUTPUT" | awk 'NR == 1 && $1 == "comic-sol" { print $2 }')
if ! printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$'; then
  echo "unable to determine a valid runtime version" >&2
  exit 1
fi
"$EXE" doctor --output-root "${COMIC_SOL_OUTPUT_ROOT:-$HOME/Comic Sol}"

INSTALL_STARTED=1
VERSIONS="$INSTALL_ROOT/versions"
TARGET="$VERSIONS/$VERSION"
TARGET_BACKUP="$VERSIONS/.${VERSION}.rollback"
STABLE_RUNTIME="$INSTALL_ROOT/bin"
STABLE_BACKUP="$INSTALL_ROOT/.bin.rollback"
mkdir -p "$VERSIONS"
rm -rf -- "$TARGET_BACKUP" "$STABLE_BACKUP"
if [ -f "$INSTALL_ROOT/active-version" ]; then
  PREVIOUS_POINTER=1
  PREVIOUS_VERSION=$(cat "$INSTALL_ROOT/active-version")
fi
mv -- "$RUNTIME" "$TARGET.new"
if [ -d "$TARGET" ]; then mv -- "$TARGET" "$TARGET_BACKUP"; fi
mv -- "$TARGET.new" "$TARGET"
TARGET_PUBLISHED=1
cp -R -- "$TARGET" "$INSTALL_ROOT/bin.new"
if [ -d "$STABLE_RUNTIME" ]; then mv -- "$STABLE_RUNTIME" "$STABLE_BACKUP"; fi
mv -- "$INSTALL_ROOT/bin.new" "$STABLE_RUNTIME"
STABLE_PUBLISHED=1
printf '%s\n' "$VERSION" > "$INSTALL_ROOT/active-version.new"
mv -- "$INSTALL_ROOT/active-version.new" "$INSTALL_ROOT/active-version"
printf '%s\n%s\n%s\n' "$INSTALL_MARKER_MAGIC" "$VERSION" "$INSTALL_ROOT" > "$INSTALL_ROOT/.comic-sol-install.new"
mv -- "$INSTALL_ROOT/.comic-sol-install.new" "$INSTALL_ROOT/$INSTALL_MARKER_NAME"
COMMITTED=1
for backup in "$STABLE_BACKUP" "$TARGET_BACKUP"; do
  if ! rm -rf -- "$backup"; then
    echo "Could not remove rollback backup '$backup'" >&2
  fi
done

echo "Installed unsigned Comic Sol $VERSION at $INSTALL_ROOT"
echo "Add $INSTALL_ROOT/bin to PATH. User projects are outside this directory."
