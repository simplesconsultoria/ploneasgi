---
myst:
  html_meta:
    "description": "Why a Plone download holds a WSGI worker thread for the client's whole transfer, and why ASGI is the first architecture that avoids it."
    "property=og:description": "Why a Plone download holds a WSGI worker thread for the client's whole transfer, and why ASGI is the first architecture that avoids it."
    "property=og:title": "Why ASGI"
---

(concepts-why-asgi)=

# Why ASGI

<!-- source: README.md, benchmarks/, tests/bench/ -->

Under WSGI, a large download holds a worker thread for the duration of the *client's transfer*, not the duration of the work.
That single sentence is the reason ploneasgi exists, and every design decision in it is measured against it.

## A thread per transfer

A WSGI server gives each request a thread from a fixed pool, and the thread stays with the request until the response is fully written.
For a rendered page, that is the time Plone spends rendering, plus a few milliseconds to write it.
For a file, it is something else entirely.

`plone.namedfile` streams a blob in chunks, and each chunk blocks until the socket accepts it.
The socket accepts it at the speed of the client's connection.
One visitor on a slow mobile connection downloading a 50 MB file pins one thread for minutes.
waitress runs four threads by default, so four such visitors take the site down: every page request queues behind downloads that are mostly waiting on the network.

This is pre-existing Plone behavior, not something ASGI introduced.
It was measured on one laptop with waitress, with a 50 MB blob and 12 page probes per round:

| Server | Concurrent slow downloads | Page requests answered |
| --- | ---: | ---: |
| waitress | 3 | 12 / 12 |
| waitress | **4** (its `threads`) | **0 / 12** |
| uvicorn and a2wsgi | 4 | 0 / 12 |
| uvicorn and ploneasgi | 16 | 12 / 12 |

The result is categorical, not a throughput number.
It is the kind of result that survives being measured on a different machine.

## Zope already describes the fix

Zope has known how this ought to work since Zope 2.
`ZPublisher.Iterators.IStreamIterator` says it in its docstring:

> After the application finishes interpreting a request and returns an iterator to be processed asynchronously, it closes the ZODB connection.

ZPublisher does exactly that.
By the time a stream iterator leaves the WSGI call, the ZODB connection is closed and the transaction is over.
Reading the remaining bytes needs a file handle, not a database connection, and not a Plone thread.

What was missing was a server that could process the iterator asynchronously.
A WSGI server cannot, by construction: the WSGI interface is a blocking call that returns an iterable, and the server's thread iterates it.

## What ASGI changes

An ASGI server runs an event loop.
Waiting for a slow client costs the event loop nothing: it is one more socket it is not writing to yet.

ploneasgi uses that.
It calls the Zope WSGI application in a worker thread, takes the returned iterable back to the event loop, and pulls one chunk at a time in a short thread hop.
The worker thread is released as soon as Zope returns, and waiting on the client happens on the loop, holding nothing.
{doc}`the-adapter` describes the split in detail.

## Why not an existing adapter

`a2wsgi` mounts WSGI apps under ASGI servers, and it is the obvious first choice.
It does not change the result: in the table above it starves at four slow downloads, just like waitress.

Reading `a2wsgi/wsgi.py` shows why.
It runs the whole WSGI call, *including iterating the body*, in a worker thread, and pushes each chunk back to the event loop with a blocking cross-thread round trip.
The thread is still held until the client has the last byte.
The adapter is ASGI on the outside and WSGI on the inside, where it matters.

The round trip also costs throughput.
On one laptop, streaming a 50 MB body over loopback with the two adapters interleaved over three cycles, ploneasgi moved 1,306, 1,399, and 1,358 MiB/s, and a2wsgi 588, 611, and 583.
The ranges do not overlap, which is the only reason to quote them; they are still a warm-cache loopback number, not a production profile.

## How the claim stays honest

The claim is tested on every change, by assertions that cannot pass by accident:

- the starvation gate asserts 12/12 page probes under ploneasgi and 0/12 under a2wsgi, on a stand-in WSGI application, on a real Plone site, and on ZEO, RelStorage, and PGJsonb;
- the a2wsgi half is a falsifier: if it ever stops starving, the downloads are no longer slow enough to hold a thread, and the ploneasgi half has become a tautology;
- a deliberate mutation that holds a worker for the whole response fails the gate with 12 of 12 probes lost.

{doc}`/how-to-guides/reproduce-the-benchmarks` runs the same measurements on your machine.

## Related

- {doc}`the-adapter` explains how the adapter splits work from transfer.
- {doc}`storages` explains where a networked storage's blob fetch lands.
- {doc}`/tutorials/serve-a-plone-site` serves a site under ploneasgi.
