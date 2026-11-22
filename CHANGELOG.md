# Changelog

All notable changes to the cost-onprem Helm chart are documented in this file.

## Unreleased

### Fixed

- **Connection budget satisfies the product's per-pod rule ([#898](https://github.com/pgarciaq/ros-ocp-backend/issues/898)):**
  `ros.dbMaxConns` defaulted to 5 while the product's default worker fan-out
  (2 download × 3 kafka = 6 in-flight) needs `dbMaxConns − 2 ≥ 6` — every
  processor start logged a connection-pool warning that was then ignored.
  The default is now 10 (the product's own `defaultDBMaxConns`), the
  connection-budget comments account for the processor's per-pod rule and
  the recomputed totals, and chart CI asserts the rendered
  `ROS_DB_MAX_CONNS` satisfies the rule so the value cannot silently
  regress.
- **`database.postgresqlConfiguration` applies on every image family ([#899](https://github.com/pgarciaq/ros-ocp-backend/issues/899)):**
  the tuning ConfigMap only ever worked on sclorg/RHEL images (their
  `postgresql-cfg/` init hook); on the library `postgres` image — the
  locally-built default on airgapped/aarch64 deployments — nothing read
  the mounted conf, silently. The values now also render as
  `ALTER SYSTEM SET` init SQL (`init-tuning.sh`) in the database
  init-scripts directory, which every image family runs and which
  persists to `postgresql.auto.conf`. Init scripts run on first
  initialization only; see `docs/operations/database-tuning.md` for the
  dual-path semantics and the manual `ALTER SYSTEM` escape hatch for
  existing databases.

### Added

- PostgreSQL tuning exposed via Helm values: `database.resources`, `database.storage.size`, and `database.postgresqlConfiguration` (ConfigMap mounted to sclorg `postgresql-cfg/`). See `docs/operations/database-tuning.md`.
- Optional HorizontalPodAutoscaler for the ROS API deployment (`ros.api.autoscaling`). Disabled by default; enable in production values overrides when metrics-server (or equivalent) is available.
