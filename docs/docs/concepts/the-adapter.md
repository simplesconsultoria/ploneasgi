---
myst:
  html_meta:
    "description": "How the ploneasgi adapter separates the work of a request from its transfer: two limiters, streamed bodies, client disconnects, and buffered uploads."
    "property=og:description": "How the ploneasgi adapter separates the work of a request from its transfer: two limiters, streamed bodies, client disconnects, and buffered uploads."
    "property=og:title": "The adapter"
---

(concepts-the-adapter)=

# The adapter

<!-- source: src/ploneasgi/wsgi.py, src/ploneasgi/accesslog.py -->

{class}`~ploneasgi.wsgi.WSGIMiddleware` mounts the Zope WSGI application inside an ASGI application.
It does the same job as `a2wsgi`, with the work split differently.

## Work and transfer

```text
a2wsgi      worker thread: call the app AND iterate the body,
                           blocking on the event loop per chunk
ploneasgi   worker thread: call the app, return the iterable
            event loop:    pull one chunk at a time and send it
```

The application call is one thread hop.
Producing each chunk is a short hop of its own.
The thread is therefore held for the *work* and never for the *transfer*.

The adapter recognizes three shapes of response body:

- a list or tuple of chunks, which is ZPublisher's ordinary case for a rendered page, is joined and sent in one event;
- a stream iterator with a public `streamsize`, such as `filestream_iterator`, has `streamsize` raised to the adapter's chunk size and is iterated;
- a raw file object is read in chunks, and never iterated, because iterating a binary file yields lines.

A stream iterator keeps its own file handle open.
That is what makes it safe to stream after the ZODB connection is closed: a concurrent pack or a blob-cache eviction can unlink the file, but on POSIX an open handle keeps reading it.

## Two limiters

The adapter holds two capacity limiters, and keeps them separate on purpose.

The application limiter bounds concurrent WSGI calls.
Each call may hold a ZODB connection, so this number is coordinated with the connection pool, and must not exceed `pool-size`.
{func}`~ploneasgi.app.create_app` publishes it as `app.state.zodb_limiter`, and {func}`~ploneasgi.context.run_authenticated` draws on it, so native routes share the same budget as WSGI requests.

The I/O limiter bounds concurrent body reads, and defaults to four times the workers, with a minimum of 16.
Pulling bytes off a file holds no ZODB connection.
Making downloads compete for the same slots as page renders would bring back exactly the starvation the adapter removes.

## Chunk size

Each chunk costs a thread dispatch, so the chunk size matters more here than it does inside a WSGI server.
`filestream_iterator` defaults to 64 KB, and on one laptop, over loopback, a 50 MB blob streamed at 666 MiB/s at 64 KB, 1,757 at 256 KB, and 3,758 at 1 MB.
The default is 256 KB.
Past that, the gain was measured on a warm page cache over loopback, and every in-flight response holds a chunk in memory.
A slow client also gets coarser backpressure with larger chunks.

## Clients that leave

While the body streams, the adapter also waits on `receive`.
Once the request body has been read, the next message an ASGI server delivers is `http.disconnect`, and it arrives only when the client goes.
When it does, the adapter stops pulling chunks and marks the scope, and the access log ends the line in `incomplete=client-gone`.

Without that watch, an abandoned download is read to the end: one thread hop per chunk, for a client that is gone.
uvicorn's `send` discards every chunk after a disconnect without telling the application, so nothing else would notice.

## Uploads are buffered

The request body is read completely before Zope is called.
It is kept in memory up to 512 KB, spills to a temporary file past that, and is refused with `413` past `PLONE_MAX_REQUEST_BODY_SIZE`.

`a2wsgi` streams uploads to the application instead, at the cost of a blocking round trip to the event loop on every read.
That round trip is the cost this adapter exists to remove.
An upload cut short by a disconnect never reaches Zope: a truncated body would otherwise be processed as if it were complete.

## Errors

Nothing reaches the client until the WSGI call has returned.
An exception raised by the application is therefore always turned into a `500`, even if the application had already called `start_response`.

Once the status line has been sent, it cannot be changed.
An exception while streaming propagates, the server aborts the connection, and the client sees a truncated response, which is the honest signal.
A clean final chunk would make the truncated body look complete.

## What the access log can see

The access log is ASGI middleware, above everything, rather than a WSGI filter below the router.
A WSGI-layer log, like `paste.translogger`, cannot see native routes or websockets, and it writes its line from `start_response`, before the body exists.
Under this adapter, the body streams *after* the WSGI call returns, so that log would miss the whole phase the adapter exists for.

The line keeps two timings apart.
`ttfb` is the time to the response start event: parsing, the worker hop, and Zope's render.
`duration` is that plus the transfer.
Worker starvation shows up as `ttfb` climbing; a slow client shows up in `duration` alone.
One merged number cannot tell a slow Plone from someone downloading a film over a bad connection.

## Related

- {doc}`why-asgi` explains the problem the split solves.
- {doc}`/how-to-guides/tune-concurrency` sets the limiters and the chunk size.
- {doc}`/reference/access-log` lists the log fields.
- {ref}`reference-api-wsgi` documents the module.
