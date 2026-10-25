"""Zope startup, and the import-time boundary that keeps this package testable.

Everything here runs with Plone absent. The Zope-facing behaviour of
:mod:`ploneasgi.zopeapp` is covered in ``tests/plone/test_zopeapp.py``.
"""

from pathlib import Path
from ploneasgi.settings import Settings
from ploneasgi.zopeapp import build_wsgi_app

import pytest
import subprocess
import sys


#: The modules that must not pull Zope, Plone or the ZODB into ``sys.modules``.
#: ``zcml`` is included deliberately: it needs ``zope.configuration``, which is
#: a small pure-Python closure and *not* Zope.
ZOPE_FREE_MODULES = [
    "ploneasgi",
    "ploneasgi.accesslog",
    "ploneasgi.app",
    "ploneasgi.context",
    "ploneasgi.settings",
    "ploneasgi.vhm",
    "ploneasgi.wsgi",
    "ploneasgi.zcml",
    "ploneasgi.zopeapp",
]

FORBIDDEN = ("Zope2", "Products", "AccessControl", "plone", "ZODB", "ZPublisher")


class TestStaysZopeFree:
    """The structural rule, checked where an installed Plone cannot hide it.

    ``make test-no-plone`` proves the same thing by removing Plone from the
    environment, but that check is unavailable to any job that *has* Plone
    installed — which is every job running the other half of this suite. A
    subprocess import is the version that works in both.
    """

    @pytest.mark.parametrize("module", ZOPE_FREE_MODULES)
    def test_importing_pulls_no_zope(self, module):
        code = (
            f"import {module}, sys;"
            f"leaked = sorted(n for n in sys.modules"
            f" if n.split('.')[0] in {FORBIDDEN!r});"
            f"print(','.join(leaked))"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "", (
            f"importing {module} pulled in: {result.stdout.strip()}"
        )


class TestBuildWsgiApp:
    def test_missing_zope_conf_raises_before_importing_zope(self, tmp_path):
        """The guard has to fire first — the import below it needs Plone.

        Reaching the ``ImportError`` instead would mean the check moved after
        the import, which turns a clear configuration error into a confusing
        one for anyone running without the ``plone`` extra.
        """
        settings = Settings(zope_conf=tmp_path / "nope" / "zope.conf")

        with pytest.raises(FileNotFoundError) as exc:
            build_wsgi_app(settings)

        assert "zope.conf not found" in str(exc.value)

    def test_a_directory_is_not_a_config_file(self, tmp_path):
        """``is_file()`` rather than ``exists()``: a directory must not pass."""
        settings = Settings(zope_conf=tmp_path)

        with pytest.raises(FileNotFoundError):
            build_wsgi_app(settings)

    def test_the_message_names_the_path_it_looked_at(self, tmp_path):
        missing = tmp_path / "etc" / "zope.conf"
        settings = Settings(zope_conf=missing)

        with pytest.raises(FileNotFoundError, match=str(missing)):
            build_wsgi_app(settings)


class TestSettingsDefault:
    def test_zope_conf_defaults_to_the_instance_layout(self):
        assert Settings().zope_conf == Path("instance/etc/zope.conf")
