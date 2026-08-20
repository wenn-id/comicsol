#!/usr/bin/env sh
# Install: sh install.sh --archive comic-sol.zip --sha256 <digest> --checksums SHA256SUMS --signature SHA256SUMS.sigstore.json
set -eu

INSTALL_ROOT="${COMIC_SOL_INSTALL_ROOT:-$HOME/.local/share/comic-sol}"
ARCHIVE=""
SHA256=""
CHECKSUMS=""
SIGNATURE=""
URL=""
UNINSTALL=0
COSIGN_BIN="${COMIC_SOL_COSIGN:-cosign}"
COSIGN_OIDC_ISSUER="https://token.actions.githubusercontent.com"
COSIGN_IDENTITY_REGEXP='^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/(tags/v[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?|heads/main)$'
INSTALL_LOCK_DIR=""
LOCK_HELD=0
SECURE_HANDOFF=0
SECURE_HANDOFF_SHIFT=0
SECURE_ROOT_FD=""
SECURE_PARENT_FD=""
INSTALL_MARKER_NAME=".comic-sol-install"
INSTALL_MARKER_MAGIC="comic-sol-install-v1"

marker_encode() {
  perl -e 'print unpack("H*", shift)' -- "$1"
}

secure_root_handoff() {
  if [ "${1:-}" = "--secure-handoff" ] && [ "$#" -ge 6 ]; then
    case "$2$3$4" in
      ''|*[!0-9]*) ;;
      *)
        if command -v perl >/dev/null 2>&1 && perl -e '
          sub same_dir {
            my ($fd, $other) = @_;
            open(my $handle, "<&=$fd") or return 0;
            my @left = stat($handle);
            my @right = stat($other);
            return @left && @right && $left[0] == $right[0] && $left[1] == $right[1];
          }
          my ($root_fd, $parent_fd, $caller_fd) = @ARGV;
          open(my $parent, "<&=$parent_fd") or exit 1;
          open(my $caller, "<&=$caller_fd") or exit 1;
          exit 1 unless -d $parent && -d $caller;
          exit 1 unless same_dir($root_fd, ".");
          exit 0;
        ' "$2" "$3" "$4"; then
          SECURE_HANDOFF=1
          SECURE_HANDOFF_SHIFT=6
          SECURE_ROOT_FD=$2
          SECURE_PARENT_FD=$3
          INSTALL_ROOT_DISPLAY=$(pwd -P)
          CALLER_ROOT=$6
          return 0
        fi
        ;;
    esac
    echo "refusing secure install handoff: directory capabilities could not be verified" >&2
    exit 1
  fi
  command -v perl >/dev/null 2>&1 || {
    echo "secure install root traversal requires perl" >&2
    exit 1
  }
  script_path=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")
  exec perl -MFcntl -MFile::Spec -MCwd -MErrno -e '
    use Fcntl qw(F_SETFD O_DIRECTORY O_NOFOLLOW O_RDONLY);
    my $script = shift @ARGV;
    my $root = $ENV{COMIC_SOL_INSTALL_ROOT};
    my $uninstall = grep { $_ eq "--uninstall" } @ARGV;
    my $caller = Cwd::getcwd();
    sysopen(my $caller_dir, $caller, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
      or die "refusing install root: cannot open caller directory: $!\n";
    for (my $i = 0; $i + 1 < @ARGV; $i++) {
      $root = $ARGV[$i + 1] if $ARGV[$i] eq "--install-root";
      if (($ARGV[$i] eq "--archive" || $ARGV[$i] eq "--checksums" || $ARGV[$i] eq "--signature") && defined $ARGV[$i + 1]) {
        $ARGV[$i + 1] = File::Spec->rel2abs($ARGV[$i + 1], $caller);
      }
    }
    $root = "$ENV{HOME}/.local/share/comic-sol" unless defined $root && length $root;
    my $absolute = File::Spec->canonpath(File::Spec->rel2abs($root, $caller));
    if ($^O eq "darwin") {
      for my $system_alias ("/var", "/tmp") {
        my $physical_alias = Cwd::abs_path($system_alias);
        $absolute =~ s/^\Q$system_alias\E(?=\/|$)/$physical_alias/
          if defined $physical_alias;
      }
    }
    my @parts = split m{/}, $absolute;
    sysopen(my $dir, "/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
      or die "refusing install root: cannot open filesystem root: $!\n";
    chdir($dir) or die "refusing install root: cannot enter filesystem root: $!\n";
    shift @parts;
    my $parent;
    for (my $i = 0; $i < @parts; $i++) {
      my $part = $parts[$i];
      next if $part eq "" || $part eq ".";
      my $next;
      if (!sysopen($next, $part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)) {
        if (!$uninstall && $! == Errno::ENOENT) {
          mkdir($part, 0777) or die "refusing install root: cannot create directory: $!\n";
          sysopen($next, $part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            or die "refusing install root: cannot securely enter directory: $!\n";
        } elsif ($uninstall && $! == Errno::ENOENT && $i == $#parts) {
          print "Comic Sol runtime is already removed. User projects were preserved.\n";
          exit 0;
        } else {
          die "refusing install root: path contains a symlink or is not a directory: $!\n";
        }
      }
      $parent = $dir;
      chdir($next) or die "refusing install root: cannot securely enter directory: $!\n";
      $dir = $next;
    }
    my $parent_handle = $parent || $dir;
    fcntl($dir, F_SETFD, 0) or die "cannot establish secure installer handoff: $!\n";
    fcntl($parent_handle, F_SETFD, 0) or die "cannot establish secure installer handoff: $!\n";
    fcntl($caller_dir, F_SETFD, 0) or die "cannot establish secure installer handoff: $!\n";
    exec "/bin/sh", $script, "--secure-handoff", fileno($dir), fileno($parent_handle), fileno($caller_dir), Cwd::getcwd(), $caller, @ARGV
      or die "cannot relaunch installer: $!\n";
  ' "$script_path" "$@"
}

secure_root_handoff "$@"
if [ "$SECURE_HANDOFF" -eq 1 ]; then
  shift "$SECURE_HANDOFF_SHIFT"
fi

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

reject_symlink_path() {
  path=$1
  case "$path" in
    /*) current="/"; remainder=${path#/} ;;
    *) current=$(pwd -P); remainder=$path ;;
  esac
  while [ -n "$remainder" ]; do
    component=${remainder%%/*}
    if [ "$remainder" = "$component" ]; then
      remainder=""
    else
      remainder=${remainder#*/}
    fi
    case "$component" in
      ''|.) continue ;;
      ..)
        current=$(dirname "$current")
        continue
        ;;
    esac
    if [ "$current" = "/" ]; then
      current="/$component"
    else
      current="$current/$component"
    fi
    if [ -L "$current" ]; then
      echo "refusing install root path containing symlink: $current" >&2
      return 1
    fi
  done
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
    --checksums) CHECKSUMS=$2; shift 2 ;;
    --signature) SIGNATURE=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --install-root) INSTALL_ROOT=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$SECURE_HANDOFF" -eq 1 ]; then
  INSTALL_ROOT="."
else
  if [ "$UNINSTALL" -eq 1 ]; then
    if [ -z "$INSTALL_ROOT" ]; then
      echo "refusing to uninstall: install root is not a directory" >&2
      exit 1
    fi
  elif [ -z "$SHA256" ] || [ -z "$CHECKSUMS" ] || [ -z "$SIGNATURE" ]; then
    echo "--sha256, --checksums, and --signature are required for a signed release" >&2
    exit 2
  fi
  reject_symlink_path "$INSTALL_ROOT"
  INSTALL_ROOT=$(canonical_install_root "$INSTALL_ROOT")
  INSTALL_ROOT_DISPLAY="$INSTALL_ROOT"
  mkdir -p "$INSTALL_ROOT"
  INSTALL_ROOT="."
fi

if [ "$SECURE_HANDOFF" -eq 1 ]; then
  INSTALL_LOCK_DIR="../.comic-sol-install.lock"
else
  INSTALL_LOCK_DIR="$(dirname -- "$INSTALL_ROOT_DISPLAY")/.comic-sol-install.lock"
fi
if [ "$UNINSTALL" -eq 1 ]; then
  acquire_install_lock
  trap 'release_install_lock' EXIT
  trap 'abort_uninstall' INT TERM

  CURRENT_ROOT=$CALLER_ROOT
  HOME_ROOT=$(cd -P -- "$HOME" && pwd -P)
  case "$INSTALL_ROOT_DISPLAY" in
    /) echo "refusing to uninstall from a filesystem root" >&2; exit 1 ;;
  esac
  if [ "$INSTALL_ROOT_DISPLAY" = "$HOME_ROOT" ] || [ "$INSTALL_ROOT_DISPLAY" = "$CURRENT_ROOT" ]; then
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
  EXPECTED_MARKER_ROOT=$(marker_encode "$INSTALL_ROOT_DISPLAY")
  ACTIVE_VERSION=$(sed -n '1p' "$ACTIVE_VERSION_FILE")
  if [ "$MARKER_LINE_COUNT" -ne 3 ] ||
     [ "$MARKER_MAGIC" != "$INSTALL_MARKER_MAGIC" ] ||
     [ -z "$MARKER_VERSION" ] ||
     [ "$MARKER_VERSION" != "$ACTIVE_VERSION" ] ||
     { [ "$MARKER_ROOT" != "$EXPECTED_MARKER_ROOT" ] &&
       [ "$MARKER_ROOT" != "$INSTALL_ROOT_DISPLAY" ]; }; then
    echo "refusing to uninstall: install registration is invalid; reinstall or upgrade this root first" >&2
    exit 1
  fi

  for child in bin versions .bin.rollback bin.new; do
    rm -rf -- "$INSTALL_ROOT/$child"
  done
  for child in active-version.new .comic-sol-install.new active-version "$INSTALL_MARKER_NAME"; do
    rm -f -- "$INSTALL_ROOT/$child"
  done
  install_root_name=$(basename -- "$INSTALL_ROOT_DISPLAY")
  cd ..
  if [ "$SECURE_HANDOFF" -eq 1 ]; then
    if perl -MFcntl -e '
      my ($root_fd, $name) = @ARGV;
      open(my $parent, "<&=3") or exit 1;
      chdir($parent) or exit 1;
      open(my $root, "<&=$root_fd") or exit 1;
      my @expected = stat($root);
      my @actual = lstat($name);
      exit 2 unless @expected && @actual && $expected[0] == $actual[0] && $expected[1] == $actual[1];
      exit(rmdir($name) ? 0 : 1);
    ' 3<&"$SECURE_PARENT_FD" "$SECURE_ROOT_FD" "$install_root_name" 2>/dev/null; then
      cleanup_status=0
    else
      cleanup_status=$?
    fi
    case "$cleanup_status" in
      0|1) ;;
      *) echo "refusing to uninstall: install root changed during cleanup" >&2; exit 1 ;;
    esac
  else
    rmdir -- "$install_root_name" 2>/dev/null || true
  fi
  INSTALL_LOCK_DIR=".comic-sol-install.lock"
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
  URL_PATH=${URL%%\?*}
  URL_PATH=${URL_PATH%%\#*}
  ARCHIVE_NAME=$(basename -- "$URL_PATH")
  case "$ARCHIVE_NAME" in
    ''|.|..|*[!A-Za-z0-9._-]*)
      echo "HTTPS URL must end with a safe release asset name" >&2
      exit 2
      ;;
  esac
  ARCHIVE="$TMP/$ARCHIVE_NAME"
  curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2 "$URL" -o "$ARCHIVE"
fi
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "Provide --archive PATH or --url HTTPS_URL" >&2
  exit 2
fi
if ! command -v "$COSIGN_BIN" >/dev/null 2>&1; then
  echo "cosign is required for signature verification" >&2
  exit 1
fi
if [ ! -f "$CHECKSUMS" ] || [ ! -f "$SIGNATURE" ]; then
  echo "checksum manifest and Sigstore bundle are required for signature verification" >&2
  exit 1
fi
if ! "$COSIGN_BIN" verify-blob \
  --bundle "$SIGNATURE" \
  --certificate-identity-regexp "$COSIGN_IDENTITY_REGEXP" \
  --certificate-oidc-issuer "$COSIGN_OIDC_ISSUER" \
  "$CHECKSUMS" >/dev/null; then
  echo "signature verification failed" >&2
  exit 1
fi

ACTUAL=$(sha256sum "$ARCHIVE" | cut -d ' ' -f 1 | tr '[:upper:]' '[:lower:]')
MANIFEST_DIGEST=$(awk -v name="$(basename -- "$ARCHIVE")" '$2 == name { print tolower($1); found = 1 } END { if (!found) exit 1 }' "$CHECKSUMS") || {
  echo "signed checksum manifest has no entry for archive" >&2
  exit 1
}
EXPECTED=$(printf '%s' "$SHA256" | tr '[:upper:]' '[:lower:]')
if [ "$ACTUAL" != "$MANIFEST_DIGEST" ] || [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "SHA256 does not match signed checksum manifest" >&2
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
MARKER_ROOT=$(marker_encode "$INSTALL_ROOT_DISPLAY")
printf '%s\n%s\n%s\n' "$INSTALL_MARKER_MAGIC" "$VERSION" "$MARKER_ROOT" > "$INSTALL_ROOT/.comic-sol-install.new"
mv -- "$INSTALL_ROOT/.comic-sol-install.new" "$INSTALL_ROOT/$INSTALL_MARKER_NAME"
COMMITTED=1
for backup in "$STABLE_BACKUP" "$TARGET_BACKUP"; do
  if ! rm -rf -- "$backup"; then
    echo "Could not remove rollback backup '$backup'" >&2
  fi
done

echo "Installed signed Comic Sol $VERSION at $INSTALL_ROOT"
echo "Add $INSTALL_ROOT/bin to PATH. User projects are outside this directory."
