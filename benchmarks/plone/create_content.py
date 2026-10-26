"""Create the Plone site and blob content the Plone-backed benchmarks read.

Run inside the instance's process, with Zope's own console::

    zconsole run <instance>/etc/zope.conf benchmarks/plone/create_content.py

``zconsole`` executes this file with ``app`` bound in its globals, which is why
the file has no ``main``. It is idempotent: an existing site is deleted and
rebuilt, so a manifest always describes the database next to it.

What it creates, and why each one is there:

* a root ``admin`` Manager — the account the instance recipe normally makes,
  and the one native routes must authenticate *from the Zope root*;
* the site, classic Plone with ``plone.app.contenttypes``;
* a published folder holding one large ``File`` — the slow clients' download;
* a private file — so a refused download has a target;
* a published folder used as a virtual root — the case where the last segment
  of the rewrite path is not the site.

Payloads are incompressible random bytes, so nothing between the blob and the
client can shrink the transfer and flatter one adapter.

Settings come from the environment: ``PLONE_SITE_ID`` (default ``Plone``),
``BENCH_BLOB_MB`` (default ``50``), ``BENCH_ADMIN_PASSWORD`` (default
``admin``) and ``BENCH_MANIFEST``, the JSON file to write.
"""

from AccessControl.SecurityManagement import newSecurityManager
from plone import api
from plone.namedfile.file import NamedBlobFile
from Products.CMFPlone.factory import addPloneSite
from Testing.makerequest import makerequest
from zope.component.hooks import setSite

import json
import os
import transaction


SITE_ID = os.environ.get("PLONE_SITE_ID", "Plone")
BLOB_MB = int(os.environ.get("BENCH_BLOB_MB", "50"))
ADMIN_PASSWORD = os.environ.get("BENCH_ADMIN_PASSWORD", "admin")
MANIFEST = os.environ["BENCH_MANIFEST"]


def blob(size: int, name: str) -> NamedBlobFile:
    """Build a blob of random bytes.

    :param size: Size in bytes.
    :param name: File name.
    :returns: The blob file.
    """
    return NamedBlobFile(
        data=os.urandom(size), filename=name, contentType="application/octet-stream"
    )


app = makerequest(globals()["app"])

if app.acl_users.getUserById("admin") is None:
    app.acl_users._doAddUser("admin", ADMIN_PASSWORD, ["Manager"], [])
admin = app.acl_users.getUserById("admin").__of__(app.acl_users)
newSecurityManager(app.REQUEST, admin)

if SITE_ID in app.objectIds():
    app.manage_delObjects([SITE_ID])
    transaction.commit()

portal = addPloneSite(
    app,
    SITE_ID,
    title="ploneasgi benchmark",
    extension_ids=("plone.app.contenttypes:default",),
    setup_content=False,
)
transaction.commit()
setSite(portal)

folder = api.content.create(container=portal, type="Folder", id="bench", title="B")
api.content.transition(obj=folder, to_state="published")
download = api.content.create(
    container=folder,
    type="File",
    id=f"blob-{BLOB_MB}mb",
    title=f"Blob {BLOB_MB} MB",
    file=blob(BLOB_MB * 1024 * 1024, f"blob-{BLOB_MB}mb.bin"),
)

# File has no workflow of its own; View is acquired from the container, so the
# private target is a file in a private folder.
private_folder = api.content.create(
    container=portal, type="Folder", id="private", title="Private"
)
private = api.content.create(
    container=private_folder,
    type="File",
    id="blob-private",
    title="Private blob",
    file=blob(1024 * 1024, "blob-private.bin"),
)

vroot = api.content.create(container=portal, type="Folder", id="vroot", title="V")
api.content.transition(obj=vroot, to_state="published")

transaction.commit()


def path_of(obj) -> str:
    """:returns: The object's path from the Zope root."""
    return "/".join(obj.getPhysicalPath())


manifest = {
    "site": SITE_ID,
    "admin": ["admin", ADMIN_PASSWORD],
    "download": {"path": path_of(download), "size": BLOB_MB * 1024 * 1024},
    "private": {"path": path_of(private)},
    "vroot": {"path": path_of(vroot)},
}
with open(MANIFEST, "w") as handle:
    json.dump(manifest, handle, indent=2)
print(f"manifest written to {MANIFEST}")
