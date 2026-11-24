"""Packaging regressions.

The RPM used to install the app into the *build host's* site-packages
(%{python3_sitelib}). CI builds in fedora:latest, so the published noarch
asset landed in /usr/lib/python3.15/site-packages while users on Python
3.14 never search that path: `import drivesync` failed and the launcher
died with ModuleNotFoundError. The spec now uses a version-independent
vendor dir; these tests keep it that way.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "packaging" / "drivesync.spec").read_text(encoding="utf-8")
APPIMAGE = (ROOT / "packaging" / "build-appimage.sh").read_text(
    encoding="utf-8")
METAINFO = (ROOT / "drivesync.metainfo.xml").read_text(encoding="utf-8")


def test_version_is_consistent_across_packaging_metadata():
    from drivesync import __version__
    assert f"Version:        {__version__}" in SPEC
    assert f'<release version="{__version__}"' in METAINFO
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog


def test_rpm_does_not_bake_the_build_python_into_the_path():
    assert "python3_sitelib" not in SPEC
    assert "py_purelib" not in SPEC


def test_rpm_uses_a_version_independent_vendor_dir():
    assert "%global appdir %{_prefix}/lib/drivesync" in SPEC
    assert "%global pkgdir %{appdir}/drivesync" in SPEC


def test_launcher_puts_the_vendor_dir_on_sys_path():
    # The package lives in appdir/drivesync, so the PARENT (appdir) must be
    # inserted: pointing sys.path at the package dir itself makes `import
    # drivesync` fall through to a pip copy or fail outright.
    assert 'sys.path.insert(0, "%{appdir}")' in SPEC


def test_appimage_gui_uses_the_host_pygobject():
    # A bundled CPython cannot import the distro's compiled gi extension, so
    # `gui` must run on the host interpreter after probing GTK 3 + requests.
    assert "Exec=DriveSync gui" in APPIMAGE
    assert 'gi.require_version("Gtk", "3.0")' in APPIMAGE
    assert "DRIVESYNC_PYTHON" in APPIMAGE


def test_appimage_payload_is_an_importable_package():
    # PYTHONPATH is usr/app, so the code must be in usr/app/drivesync —
    # copying it *as* usr/app made `python -m drivesync` fail for every
    # command ("No module named drivesync").
    assert 'cp -r "$ROOT/drivesync" "$APPDIR/usr/app/drivesync"' in APPIMAGE
