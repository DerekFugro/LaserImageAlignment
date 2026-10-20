"""The batch report names the code that wrote it."""
from core import version


def test_version_names_the_package_and_the_commit():
    version.app_version.cache_clear()
    v = version.app_version()
    assert v.startswith(version._package_version() + ", commit ")


def test_no_git_still_gives_a_version(monkeypatch):
    version.app_version.cache_clear()
    monkeypatch.setattr(version, "_git", lambda *a: None)
    assert "commit unknown" in version.app_version()
    version.app_version.cache_clear()


def test_dirty_tree_is_said(monkeypatch):
    version.app_version.cache_clear()
    monkeypatch.setattr(version, "_git", lambda *a: "abc1234 (2026-10-20)"
                        if a[0] == "log" else " M core/x.py")
    assert version.app_version().endswith("+ UNCOMMITTED CHANGES")
    version.app_version.cache_clear()


def test_report_header_has_the_app_line():
    from core.batch import BatchReport
    rep = BatchReport(root="r", started_utc="2026-10-20T00:00:00+00:00")
    assert any(l.startswith("app:     ") for l in rep.to_text().splitlines())
