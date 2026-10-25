"""Generate a throwaway Zope instance for benchmarking.

The Plone-backed benchmarks need a database a *separate process* can open, which
rules out the in-memory ``DemoStorage`` the test layer uses: the server under
measurement has to be a subprocess, and a DemoStorage lives and dies inside the
interpreter that made it.

Deliberately **not** ``cookiecutter-zope-instance``. That generates a full
project scaffold including the PasteDeploy ``zope.ini`` this package exists to do
without, and it would be a dependency of a library in order to produce a file the
library never reads. What Zope actually needs to open a database is about twenty
lines, and writing them here keeps the benchmark honest about the package's own
claim: environment configuration, one ``zope.conf``, no ini.

Usage::

    python benchmarks/plone/make_instance.py /tmp/bench-instance
"""

from pathlib import Path

import argparse
import sys


ZOPE_CONF = """\
%define INSTANCEHOME {home}
instancehome $INSTANCEHOME
debug-mode off
security-policy-implementation C
verbose-security off

# Must be at least the worker count the server is given, or requests block
# waiting for a connection and the benchmark measures the wrong queue.
<zodb_db main>
    cache-size 30000
    pool-size {pool_size}
    <blobstorage>
        blob-dir {home}/var/blobstorage
        <filestorage>
            path {home}/var/filestorage/Data.fs
        </filestorage>
    </blobstorage>
    mount-point /
</zodb_db>
"""
# No <eventlog>: it is not in Zope's zope.conf schema any more and fails the
# parse with "unknown type name". Logging is configured in Python by
# ploneasgi.settings.setup_logging, which is the point — this file exists only
# to name a database.

# Modelled on what cookiecutter-zope-instance generates, because the order here
# is load-bearing and not guessable: `five:loadProducts file="meta.zcml"` is what
# registers the directives the rest of the tree uses. Get it wrong and Plone's
# first `browser:viewlet` fails with "Unknown directive", pointing at a file that
# is not the problem.
SITE_ZCML = """\
<configure
    xmlns="http://namespaces.zope.org/zope"
    xmlns:meta="http://namespaces.zope.org/meta"
    xmlns:five="http://namespaces.zope.org/five">

  <include package="Products.Five" />

  <meta:redefinePermission from="zope2.Public" to="zope.Public" />
  <five:loadProducts file="meta.zcml"/>
  <five:loadProducts />
  <five:loadProductsOverrides />

  <securityPolicy component="AccessControl.security.SecurityPolicy" />
</configure>
"""


def build(home: Path, *, pool_size: int = 16) -> Path:
    """Write a minimal instance layout and return its ``zope.conf``.

    :param home: Directory to build the instance in; created if absent.
    :param pool_size: ZODB connection pool size.
    :returns: Path to the generated ``zope.conf``.
    """
    home = home.resolve()
    for sub in ("etc", "var/filestorage", "var/blobstorage"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    conf = home / "etc" / "zope.conf"
    conf.write_text(ZOPE_CONF.format(home=home, pool_size=pool_size))
    (home / "etc" / "site.zcml").write_text(SITE_ZCML)
    return conf


def main() -> int:
    """Parse arguments and build the instance.

    :returns: Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", type=Path)
    parser.add_argument("--pool-size", type=int, default=16)
    args = parser.parse_args()

    conf = build(args.home, pool_size=args.pool_size)
    print(conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
