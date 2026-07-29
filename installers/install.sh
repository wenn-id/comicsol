#!/usr/bin/env sh
set -eu

VERSION="2.0.0rc1"
INSTALL_ROOT="${COMIC_SOL_INSTALL_ROOT:-$HOME/.local/share/comic-sol}"
ARCHIVE=""
SHA256=""
URL=""
UNINSTALL=0

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
  rm -rf -- "$INSTALL_ROOT"
  echo "Comic Sol runtime removed. User projects were preserved."
  exit 0
fi

if [ -z "$SHA256" ]; then
  echo "--sha256 is required for this unsigned prerelease" >&2
  exit 2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
if [ -n "$URL" ]; then
  ARCHIVE="$TMP/comic-sol.zip"
  curl -fL --proto '=https' --tlsv1.2 "$URL" -o "$ARCHIVE"
fi
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "Provide --archive PATH or --url HTTPS_URL" >&2
  exit 2
fi

ACTUAL=$(sha256sum "$ARCHIVE" | cut -d ' ' -f 1)
if [ "$ACTUAL" != "$SHA256" ]; then
  echo "SHA256 mismatch" >&2
  exit 1
fi

STAGE="$TMP/stage"
mkdir -p "$STAGE"
case "$ARCHIVE" in
  *.zip) unzip -q "$ARCHIVE" -d "$STAGE" ;;
  *) echo "Unsupported archive; POSIX installer currently requires .zip" >&2; exit 2 ;;
esac
RUNTIME="$STAGE/comic-sol"
EXE="$RUNTIME/comic-sol"
chmod 755 "$EXE"
"$EXE" doctor --output-root "${COMIC_SOL_OUTPUT_ROOT:-$HOME/Comic Sol}"

VERSIONS="$INSTALL_ROOT/versions"
TARGET="$VERSIONS/$VERSION"
mkdir -p "$VERSIONS" "$INSTALL_ROOT"
rm -rf "$TARGET.new"
mv "$RUNTIME" "$TARGET.new"
rm -rf "$TARGET"
mv "$TARGET.new" "$TARGET"
rm -rf "$INSTALL_ROOT/bin.new" "$INSTALL_ROOT/bin.rollback"
cp -R "$TARGET" "$INSTALL_ROOT/bin.new"
if [ -d "$INSTALL_ROOT/bin" ]; then
  mv "$INSTALL_ROOT/bin" "$INSTALL_ROOT/bin.rollback"
fi
mv "$INSTALL_ROOT/bin.new" "$INSTALL_ROOT/bin"
rm -rf "$INSTALL_ROOT/bin.rollback"
printf '%s\n' "$VERSION" > "$INSTALL_ROOT/active-version.new"
mv "$INSTALL_ROOT/active-version.new" "$INSTALL_ROOT/active-version"

echo "Installed unsigned Comic Sol $VERSION at $INSTALL_ROOT"
echo "Add $INSTALL_ROOT/bin to PATH. User projects are outside this directory."
