---
myst:
  html_meta:
    "description": "The security model of ploneasgi native routes: permission ids, authentication through PAS, restricted traversal, and teardown."
    "property=og:description": "The security model of ploneasgi native routes: permission ids, authentication through PAS, restricted traversal, and teardown."
    "property=og:title": "Security of native routes"
---

(concepts-security)=

# Security of native routes

<!-- source: src/ploneasgi/context.py, src/ploneasgi/zcml.py -->

A native route runs outside ZPublisher, so none of Zope's security happens to it by accident.
Everything ZPublisher would have done has to be done on purpose, and done the same way every time.
That is why it lives in one function, {func}`~ploneasgi.context.run_authenticated`, rather than in each endpoint.

## A guard before the handler

`<ploneasgi:route />` refuses to register a route with no `permission` unless it says `unguarded="true"`.
A forgotten permission and an endpoint that is open on purpose must never look the same.

The guard checks the permission against the portal, as the connecting user, before the handler runs.
An endpoint therefore cannot forget to authorize itself.

For an HTTP route, the check and the handler run in the same context: one worker thread, one ZODB connection, one authentication.
What the guard checked is what the handler reads, from the same snapshot, as the same user.
A websocket that fails the check is closed without being accepted, because accepting first and closing after would tell an unauthorized client that the endpoint exists, and hand it a live socket, however short-lived.

## Permission ids, not titles

ZCML names permissions by id, `zope2.View` or `cmf.ManagePortal`, as every other directive does.
Zope's security machinery knows them only by title, `View` or `Manage portal`, and treats any other string as an unknown permission granted to Manager alone.

Passing the id straight through would look correct to the one person most likely to test it, the Manager, while refusing everyone else.
The guard resolves the id to its title through the `IPermission` utility.
An id that resolves to nothing refuses every request, and logs an error, rather than falling back to Manager-only.

## Authentication through the user folders

Credentials are never parsed here.
The guard and `run_authenticated` build a real ZPublisher request from the ASGI scope and hand it to `acl_users.validate()`, so every PAS plugin a site has installed works unchanged: basic authentication, cookies, JWT, OIDC.

Each acquired user folder is asked in turn, innermost first, as ZPublisher does during traversal.
That is what lets the Zope *root* user, the emergency `admin` account an instance creates, act inside a site.
Asking only the portal's `acl_users` looks correct, and locks that account out of every native route without an error.
The test suite was once fully green while it did exactly that; only a running server found it.

Credentials that do not authenticate are not an error.
The request proceeds as Anonymous, as it would in ZPublisher, and the permission decides.

## Restricted traversal

{meth}`PloneContext.traverse <ploneasgi.context.PloneContext.traverse>` uses `restrictedTraverse`, which checks permissions at every step of the path.

The tempting alternative, `unrestrictedTraverse` followed by a permission check on the object it returns, skips the checks on everything in between.
That shape is the shape of most path-traversal disclosure bugs: a private folder's public-looking child becomes reachable through the folder.

Finding the site is the one unrestricted walk, and it is safe for a specific reason.
It only locates the portal: nothing it passes through reaches the caller, and every access to content goes through `traverse`.

## Nothing outlives the call

The work runs in a worker thread, with a ZODB connection, a site, and a security manager.
All three are torn down before the thread is returned, in a `finally` block.

The security manager is the one that matters most.
It is thread-local, and a leaked one would apply to whatever request the thread serves next: a privilege escalation, not a leak.

Every call ends in `transaction.abort()`.
A context reads and commits nothing, and anything the work dirties by accident is discarded, so a read-only endpoint cannot become a writer by mistake.
Writing needs its own answer to conflict errors and retries, which a read never has to give, and is not enabled.

`work`, and an HTTP handler's response, must hold plain data.
A persistent object that reaches the event loop belongs to a connection that has been closed.
A blob is the exception that proves the rule: open it in the thread with `openDetached()`, which returns a plain file on the committed blob, and stream that.

## The cost of the guard

Measured in-process on a warm FileStorage, the permission check costs a fraction of a millisecond.
The thread hop around it is what costs, because the hop draws on the same limiter as page renders.

An HTTP handler is a sync function that runs inside the guard's own call for that reason.
Before, the guard and an async handler each called `run_authenticated`: two thread hops, two connections, two authentications, and under load two waits for a limiter slot behind page renders.
`make bench-routes` serves both shapes side by side.
On one laptop, with four workers, five cycles and a warm FileStorage, the single call served a median of 1.62 times the requests per second of the two calls on its own, and 1.37 times beside page renders.
The spreads of the two shapes did not overlap.

A websocket still checks in a call of its own before it is accepted.
Its handler runs for as long as the client stays connected, and must not hold a worker thread or a connection for that long.

## How it is tested

The security tests are mutation-tested, because a security test that has never failed proves nothing.
Swapping `restrictedTraverse` for `unrestrictedTraverse` must fail the file-disclosure test, and restoring the old id-as-title permission check must fail the positive cases.
The paths that touch authentication or traversal are also checked against a running instance, over a socket.

## Related

- {doc}`native-routes` explains where routes sit and which site they enter.
- {doc}`/how-to-guides/read-content-from-a-route` uses `run_authenticated`.
- {doc}`/reference/route-directive` lists the directive's attributes.
- {ref}`reference-api-context` documents the module.
