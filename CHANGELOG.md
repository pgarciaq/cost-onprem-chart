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
### Added

- PostgreSQL tuning exposed via Helm values: `database.resources`, `database.storage.size`, and `database.postgresqlConfiguration` (ConfigMap mounted to sclorg `postgresql-cfg/`). See `docs/operations/database-tuning.md`.
- Optional HorizontalPodAutoscaler for the ROS API deployment (`ros.api.autoscaling`). Disabled by default; enable in production values overrides when metrics-server (or equivalent) is available.
