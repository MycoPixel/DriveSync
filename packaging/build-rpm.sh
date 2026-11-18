#!/usr/bin/env bash
# Build the DriveSync RPM.
#
# Needs: rpmbuild (dnf install rpm-build) — run on Fedora/RHEL/openSUSE.
#   ./packaging/build-rpm.sh    -> dist/drivesync-<ver>-1.noarch.rpm
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); from drivesync import __version__; print(__version__)")"
SOURCES="$(mktemp -d)"
trap 'rm -rf "$SOURCES"' EXIT

echo "==> Building drivesync ${VERSION} RPM"

# rpmbuild wants a tarball named drivesync-<version>.tar.gz at the top level
TOP="$SOURCES/drivesync-${VERSION}"
mkdir -p "$TOP"
cp -r "$ROOT/drivesync" "$ROOT/packaging" "$ROOT/README.md" "$ROOT/LICENSE" \
      "$ROOT/CHANGELOG.md" "$ROOT/drivesync.desktop" \
      "$ROOT/drivesync.metainfo.xml" "$TOP/"
# normalize modes: docs/specs read-only, scripts executable — otherwise
# rpmbuild's shebang-mangling complains about executable files without one
find "$TOP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$TOP/.pytest_cache" "$TOP/dist"
chmod 644 "$TOP/README.md" "$TOP/LICENSE" "$TOP/drivesync.desktop" \
          "$TOP/drivesync.metainfo.xml" "$TOP/CHANGELOG.md" \
          "$TOP/packaging/drivesync.spec" "$TOP/packaging/README.md"
find "$TOP" -name '*.py' -exec chmod 644 {} +
find "$TOP" -name '*.sh' -exec chmod 755 {} +
tar -czf "$SOURCES/drivesync-${VERSION}.tar.gz" -C "$SOURCES" "drivesync-${VERSION}"

RPMROOT="$SOURCES/rpmbuild"
mkdir -p "$RPMROOT"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp "$SOURCES/drivesync-${VERSION}.tar.gz" "$RPMROOT/SOURCES/"
cp "$HERE/drivesync.spec" "$RPMROOT/SPECS/"
# the spec's Version tag must match drivesync/__init__.py: a stale
# hardcode silently makes %prep look for the OLD tarball ("No such file
# or directory") and names the built rpm wrongly. Inject it every build.
sed -i "s/^Version:.*/Version:        ${VERSION}/" "$RPMROOT/SPECS/drivesync.spec"
# the top %changelog entry should describe this release — warn, not fail
TOPENTRY="$(sed -n 's/^\* .* - \([0-9][0-9.]*\)-1$/\1/p' \
            "$RPMROOT/SPECS/drivesync.spec" | head -1)"
if [ "$TOPENTRY" != "$VERSION" ]; then
  echo "warning: spec %changelog top entry is ${TOPENTRY}-1 but the " \
       "package version is ${VERSION}-1 — add a ${VERSION}-1 entry to " \
       "packaging/drivesync.spec" >&2
fi

rpmbuild --define "_topdir $RPMROOT" -bb "$RPMROOT/SPECS/drivesync.spec"

mkdir -p "$ROOT/dist"
cp "$RPMROOT"/RPMS/noarch/drivesync-"${VERSION}"-1*.rpm "$ROOT/dist/"
echo "==> $ROOT/dist/drivesync-${VERSION}-1*.rpm"
