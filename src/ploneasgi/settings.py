"""Environment-only configuration, replacing the PasteDeploy ``.ini``."""

from dataclasses import dataclass
from dataclasses import field
from dotenv import load_dotenv
from pathlib import Path
from ploneasgi.vhm import DEFAULT_PREFIX

import logging
import os


TRUTHY = frozenset({"1", "true", "on", "yes"})

#: Mirrors ``formatter_generic`` from the generated ``zope.ini``, so log output
#: stays recognizable after dropping PasteDeploy.
LOG_FORMAT = (
    "%(asctime)s %(levelname)-7.7s [%(name)s:%(lineno)s][%(threadName)s] %(message)s"
)


def load_env(path: Path | str | None = None) -> None:
    """Load a ``.env`` file, if present.

    Must be called before any Zope import or configuration read.
    ``load_dotenv`` does not override variables already in the environment, so a
    container's real environment always wins over a local file.

    :param path: Path to the ``.env`` file; defaults to ``.env`` in the cwd.
    """
    load_dotenv(path or Path.cwd() / ".env")


def flag(name: str, default: str = "") -> bool:
    """Read a boolean toggle from the environment.

    :param name: Environment variable name.
    :param default: Value assumed when the variable is unset.
    :returns: ``True`` when the value is one of ``1``, ``true``, ``on``, ``yes``.
    """
    return os.environ.get(name, default).strip().lower() in TRUTHY


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for an ASGI-hosted Plone.

    Built from the environment by :meth:`from_env` rather than from class-level
    defaults, so tests can construct an explicit instance without touching
    ``os.environ``.
    """

    #: Interface to bind. ``0.0.0.0`` is IPv4-only under uvicorn, unlike
    #: waitress's ``localhost``, which also binds IPv6.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080
    #: Concurrent WSGI calls. Must not exceed the ZODB ``pool-size`` or requests
    #: block waiting for a database connection.
    concurrency: int = 4
    access_log: bool = False
    log_level: str = "INFO"
    zope_conf: Path = field(default_factory=lambda: Path("instance/etc/zope.conf"))
    #: Bytes pulled per thread hop when streaming a response body.
    chunk_size: int = 256 * 1024
    #: Path prefix reserved for native ASGI routes. Every ZCML-registered route
    #: lives under it, which is what lets a deployment keep one proxy rule.
    route_prefix: str = DEFAULT_PREFIX
    #: Site entered by native routes when a request arrives with no
    #: VirtualHostMonster rewrite — direct access, i.e. development. Behind a
    #: proxy the rewrite is authoritative and this is unused.
    site_id: str = "Plone"
    #: Largest request body accepted, in bytes; larger ones get a 413 before
    #: Zope runs. waitress's default. ``0`` disables the limit.
    max_body_size: int = 1024 * 1024 * 1024

    @classmethod
    def from_env(cls, base: Path | None = None) -> Settings:
        """Build settings from environment variables.

        :param base: Directory that relative default paths resolve against.
        :returns: A populated :class:`Settings` instance.
        """
        base = base or Path.cwd()
        return cls(
            host=os.environ.get("PLONE_HOST", "0.0.0.0"),  # noqa: S104
            port=int(os.environ.get("PLONE_PORT", "8080")),
            concurrency=int(os.environ.get("PLONE_CONCURRENCY", "4")),
            access_log=flag("PLONE_ACCESS_LOG"),
            log_level=os.environ.get("PLONE_LOG_LEVEL", "INFO").upper(),
            zope_conf=Path(
                os.environ.get(
                    "PLONE_ZOPE_CONF", str(base / "instance" / "etc" / "zope.conf")
                )
            ),
            chunk_size=int(os.environ.get("PLONE_BLOB_CHUNK_SIZE", 256 * 1024)),
            route_prefix=os.environ.get("PLONE_ROUTE_PREFIX", DEFAULT_PREFIX),
            site_id=os.environ.get("PLONE_SITE_ID", "Plone"),
            max_body_size=int(
                os.environ.get("PLONE_MAX_REQUEST_BODY_SIZE", 1024 * 1024 * 1024)
            ),
        )


def setup_logging(settings: Settings) -> None:
    """Configure logging for the no-ini case.

    ``runwsgi`` normally hands the PasteDeploy ``.ini`` to
    :func:`logging.config.fileConfig`, which is where Zope's event log and access
    log get configured. Dropping the ini drops that too, so configure a plain
    stream handler instead and let the container collect stdout.

    Uses ``force=True`` because Zope's startup installs its own handlers: without
    it, ``basicConfig`` silently does nothing whenever this is called after
    :func:`ploneasgi.zopeapp.build_wsgi_app`, and the access log emits nothing at
    all. That ordering is easy to hit — it is what a project's ``server.py`` does
    when it builds the WSGI app itself to collect ZCML routes.

    :param settings: Settings providing the desired log level.
    """
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT, force=True)
