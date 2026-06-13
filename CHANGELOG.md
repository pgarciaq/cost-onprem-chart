# Changelog

All notable changes to the cost-onprem Helm chart are documented in this file.

## Unreleased

### Added

- Optional HorizontalPodAutoscaler for the ROS API deployment (`ros.api.autoscaling`). Disabled by default; enable in production values overrides when metrics-server (or equivalent) is available.
