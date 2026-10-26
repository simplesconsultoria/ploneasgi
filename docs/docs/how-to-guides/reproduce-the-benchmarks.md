---
myst:
  html_meta:
    "description": "Run the ploneasgi starvation gate, throughput comparison, and storage matrix on your own machine."
    "property=og:description": "Run the ploneasgi starvation gate, throughput comparison, and storage matrix on your own machine."
    "property=og:title": "Reproduce the benchmarks"
---

(how-to-reproduce-the-benchmarks)=

# Reproduce the benchmarks

<!-- source: Makefile, benchmarks/, tests/bench/ -->

This guide shows you how to run the measurements behind ploneasgi's claims, from a checkout of the repository.

## Run the starvation gate

The gate asserts that page requests are answered while slow clients download a large file:

```shell
make bench-test
```

It serves a stand-in WSGI application under ploneasgi and under a2wsgi, and expects 12 of 12 page probes under ploneasgi and 0 of 12 under a2wsgi.
Both halves must hold.

To run the same gate against a real Plone instance, which the harness generates:

```shell
make bench-plone
```

To run it on ZEO, RelStorage, and PGJsonb, with PostgreSQL in a throwaway Docker container:

```shell
make bench-storage
```

Set `BENCH_PG_DSN` to use an existing PostgreSQL server instead of Docker.

## Measure one adapter

Each mode starts a server, drives load against it, and stops it:

```shell
make bench-starve ADAPTER=a2wsgi SLOW_CLIENTS=4
make bench-throughput ADAPTER=ploneasgi REPS=3
make bench-capacity ADAPTER=ploneasgi SLOW_CLIENTS=16
```

`ADAPTER` is `ploneasgi` or `a2wsgi`.

## Compare throughput

Run the two adapters in alternation, a fresh server per run:

```shell
make bench-compare REPS=5
```

Report the spread across cycles, not the mean of one.
Record the machine and conditions with any number you quote.

## Serve the stand-in app

To point another load tool at the stand-in application, serve it on port 8091:

```shell
make bench-serve ADAPTER=ploneasgi
```

Probe `127.0.0.1`, not `localhost`, which can resolve to an IPv6 address the server does not listen on.

## Related

- {doc}`/concepts/why-asgi` explains what the gate proves.
- {doc}`/concepts/storages` reports the storage measurements.
- {doc}`develop-ploneasgi` sets up the checkout.
