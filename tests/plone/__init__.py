"""The half of the suite that needs a real Plone site.

Shared values for it live here; fixtures live in ``conftest.py``.
"""

import base64


#: Created by ``@pytest.mark.portal`` on the tests that need content another
#: connection must be denied. Private, so Anonymous cannot view it.
PRIVATE_DOCUMENT = {
    "type": "Document",
    "id": "secret",
    "title": "Secret",
    "_review_state": "private",
}

#: A Site Administrator, created by the ``site_admin`` fixture.
SITE_ADMIN = ("siteadmin", "secret-site-admin")


def basic_auth(username: str, password: str) -> list[tuple[bytes, bytes]]:
    """Build a Basic auth header.

    :param username: The user name.
    :param password: The password.
    :returns: A one-element header list.
    """
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return [(b"authorization", f"Basic {token}".encode())]
