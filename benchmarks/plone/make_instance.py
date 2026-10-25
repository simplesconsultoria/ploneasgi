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
    python benchmarks/plone/make_instance.py /tmp/bench-instance \\
        --routes-zcml benchmarks/plone/routes.zcml
    python benchmarks/plone/make_instance.py /tmp/zeo-instance \\
        --storage zeo --zeo-address 127.0.0.1:8100
    python benchmarks/plone/make_instance.py /tmp/pg-instance \\
        --storage pgjsonb --dsn "host=127.0.0.1 dbname=zodb user=zodb password=zodb"
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
{storage}
    mount-point /
</zodb_db>
"""
# No <eventlog>: it is not in Zope's zope.conf schema any more and fails the
# parse with "unknown type name". Logging is configured in Python by
# ploneasgi.settings.setup_logging, which is the point — this file exists only
# to name a database.

#: The storage section of ``<zodb_db main>``, one per supported backend.
#:
#: Each networked one is the section ``cookiecutter-zope-instance`` generates for
#: that storage, trimmed to the keys whose defaults matter here. Blobs are
#: deliberately *not* shared: every backend but ``file`` fetches a blob into a
#: local cache on first read, and that fetch is what this matrix exists to
#: observe. A shared blob directory would turn ZEO and RelStorage back into the
#: ``file`` case.
STORAGES = {
    "file": """\
    <blobstorage>
        blob-dir {home}/var/blobstorage
        <filestorage>
            path {home}/var/filestorage/Data.fs
        </filestorage>
    </blobstorage>""",
    "zeo": """\
    <zeoclient>
        server {zeo_address}
        name 1
        shared-blob-dir false
        blob-dir {home}/var/blobcache
        cache-size 128MB
    </zeoclient>""",
    "relstorage": """\
    %import relstorage
    <relstorage>
        shared-blob-dir false
        blob-dir {home}/var/blobcache
        keep-history true
        create-schema true
        <postgresql>
            driver psycopg2
            dsn {dsn}
        </postgresql>
    </relstorage>""",
    # No blob directory at all: blobs below blob-threshold live in a bytea
    # column, and every read is materialized into a bounded local cache.
    "pgjsonb": """\
    %import zodb_pgjsonb
    <pgjsonb>
        dsn {dsn}
        blob-temp-dir {home}/var/blobtemp
    </pgjsonb>""",
}

#: What each storage needs from the command line, beyond the instance home.
REQUIRES = {"zeo": "zeo_address", "relstorage": "dsn", "pgjsonb": "dsn"}

#: The ZEO server's own configuration, for ``runzeo -C``. The storage it serves
#: is named ``1``, as the client section above expects.
ZEO_CONF = """\
<zeo>
    address {zeo_address}
</zeo>
<filestorage 1>
    path {home}/var/zeo/Data.fs
    blob-dir {home}/var/zeo/blobs
</filestorage>
"""

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
{extra}
  <securityPolicy component="AccessControl.security.SecurityPolicy" />
</configure>
"""

#: Loaded after the products, so the permissions a route names already exist.
ROUTES_INCLUDE = """
  <include package="ploneasgi" file="meta.zcml" />
  <include file="{path}" />
"""


def build(
    home: Path,
    *,
    pool_size: int = 16,
    storage: str = "file",
    routes_zcml: Path | None = None,
    zeo_address: str | None = None,
    dsn: str | None = None,
) -> Path:
    """Write a minimal instance layout and return its ``zope.conf``.

    For ``zeo`` it also writes ``etc/zeo.conf``; starting that server, or the
    PostgreSQL a DSN points at, is the caller's job.

    :param home: Directory to build the instance in; created if absent.
    :param pool_size: ZODB connection pool size.
    :param storage: Key of :data:`STORAGES` naming the database backend.
    :param routes_zcml: ZCML file registering native routes, included last.
    :param zeo_address: ``host:port`` of the ZEO server, for ``zeo``.
    :param dsn: libpq connection string, for ``relstorage`` and ``pgjsonb``.
    :returns: Path to the generated ``zope.conf``.
    :raises ValueError: On an unknown storage, or one missing what it needs.
    """
    if storage not in STORAGES:
        raise ValueError(f"unknown storage {storage!r}; choose from {sorted(STORAGES)}")
    options = {"zeo_address": zeo_address, "dsn": dsn}
    needed = REQUIRES.get(storage)
    if needed and not options[needed]:
        raise ValueError(f"storage {storage!r} needs --{needed.replace('_', '-')}")

    home = home.resolve()
    for sub in ("etc", "var/filestorage", "var/blobstorage", "var/zeo"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    section = STORAGES[storage].format(home=home, **options)
    if storage == "zeo":
        zeo_conf = ZEO_CONF.format(home=home, zeo_address=zeo_address)
        (home / "etc" / "zeo.conf").write_text(zeo_conf)
    conf = home / "etc" / "zope.conf"
    conf.write_text(ZOPE_CONF.format(home=home, pool_size=pool_size, storage=section))

    extra = ROUTES_INCLUDE.format(path=routes_zcml.resolve()) if routes_zcml else ""
    (home / "etc" / "site.zcml").write_text(SITE_ZCML.format(extra=extra))
    return conf


def main() -> int:
    """Parse arguments and build the instance.

    :returns: Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", type=Path)
    parser.add_argument("--pool-size", type=int, default=16)
    parser.add_argument("--storage", choices=sorted(STORAGES), default="file")
    parser.add_argument("--routes-zcml", type=Path, default=None)
    parser.add_argument("--zeo-address", default=None, help="host:port, for zeo")
    parser.add_argument("--dsn", default=None, help="for relstorage and pgjsonb")
    args = parser.parse_args()

    conf = build(
        args.home,
        pool_size=args.pool_size,
        storage=args.storage,
        routes_zcml=args.routes_zcml,
        zeo_address=args.zeo_address,
        dsn=args.dsn,
    )
    print(conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
