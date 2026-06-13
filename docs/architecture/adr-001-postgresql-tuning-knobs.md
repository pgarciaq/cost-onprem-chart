# ADR-001: Curated PostgreSQL Tuning Knobs in the Helm Chart

## Status

Accepted

## Context

The bundled PostgreSQL container for on-prem deployments ships with stock defaults (512 MiB RAM, 30 GiB PVC, `shared_buffers` ~128 MB, `work_mem` ~4 MB, `max_connections` 100). Production fleets with 5k–15k+ monitored containers and 45-day sample retention (E-2) require operator-controlled tuning without forking the chart.

Three integration options exist for the Red Hat / sclorg PostgreSQL 16 image:

1. **Environment variables** — Native support for `POSTGRESQL_MAX_CONNECTIONS`, `POSTGRESQL_SHARED_BUFFERS`, and `POSTGRESQL_EFFECTIVE_CACHE_SIZE` only. Auto-tuning derives memory settings from the container memory limit when env vars are unset.
2. **Raw `postgresql.conf` mount** — Mount a complete config file into the data directory; fragile across image upgrades and conflicts with image-managed `postgresql.auto.conf`.
3. **`postgresql-cfg/` include directory** — sclorg images append `*.conf` files from `/opt/app-root/src/postgresql-cfg/` to the end of `postgresql.conf`, overriding earlier defaults without replacing the full file.

## Decision

Expose a **curated map** of well-understood settings via `database.postgresqlConfiguration` in `values.yaml`, rendered into a ConfigMap and mounted at `/opt/app-root/src/postgresql-cfg/custom-postgresql.conf`.

Container resources and PVC size are exposed separately as `database.resources` and `database.storage.size`.

## Consequences

### Positive

- Operators tune production deployments via Helm values without rebuilding images.
- Settings are validated at chart review time (known keys, documented sizing profiles).
- sclorg include mechanism survives image upgrades better than replacing the full `postgresql.conf`.
- Backward compatible: `resources.database` and `database.server.storage.size` remain as fallbacks.

### Negative

- Advanced PostgreSQL settings not in the curated map require a chart change or external PostgreSQL (`database.deploy: false`).
- Operators must keep memory-related settings (`shared_buffers`, `effective_cache_size`, `work_mem`) aligned with `database.resources.limits.memory` manually — the chart does not auto-derive them (unlike sclorg env-var auto-tuning).

## Alternatives Considered

| Alternative | Why not chosen |
|-------------|----------------|
| Env vars only | Covers 3 of 8 required knobs; no `work_mem`, `maintenance_work_mem`, or autovacuum settings |
| Full `postgresql.conf` ConfigMap | Replaces image defaults; higher risk on upgrade; harder to document safe subsets |
| S2I image extension | Requires custom image build pipeline; out of scope for chart-only operators |

## References

- [sclorg postgresql-container README](https://github.com/sclorg/postgresql-container/blob/master/src/root/usr/share/container-scripts/postgresql/README.md) — `postgresql-cfg/` and env var documentation
- [Database Tuning Guide](../operations/database-tuning.md) — sizing profiles and examples
