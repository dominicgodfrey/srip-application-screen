"""Smoke test: the package imports and exposes a version string."""

import srip_filter


def test_package_has_version() -> None:
    assert isinstance(srip_filter.__version__, str)
    assert srip_filter.__version__
