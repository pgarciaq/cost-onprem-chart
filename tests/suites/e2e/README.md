# E2E Test Suite

End-to-end tests that validate the complete data flow pipeline.

## YAML-Driven Scenario Tests (`test_scenarios.py`)

Framework for testing different OCP workload patterns with known expected values.

| Test Class | Test | Description |
|------------|------|-------------|
| `TestScenarioDefinitions` | `test_scenario_yaml_valid[scenario]` | YAML is parseable for each scenario |
| | `test_scenario_has_expected_values[scenario]` | Expected values are defined |
| | `test_all_scenarios_have_descriptions` | All scenarios have documentation |
| `TestScenarioExpectedValues` | `test_pod_count_expected[scenario-count]` | Pod counts match expected |
| | `test_namespace_count_expected[scenario-count]` | Namespace counts match |
| | `test_cpu_hours_calculation[hours]` | CPU hours scale correctly (1h, 24h, 168h) |
| | `test_memory_gb_hours_calculation[hours]` | Memory GB-hours scale correctly |
| `TestScenarioYAMLStructure` | `test_yaml_has_ocp_generator[scenario]` | Uses OCPGenerator |
| | `test_yaml_has_required_fields[scenario]` | Has start_date, end_date, nodes |
| | `test_yaml_dates_are_dynamic[scenario]` | Dates are injected, not hardcoded |

**Available Scenarios**:

| Scenario | Description | Pods | Namespaces | CPU | Memory |
|----------|-------------|------|------------|-----|--------|
| `minimal_ocp_pod_only` | Single pod baseline | 1 | 1 | 0.5 cores | 1 GiB |
| `multi_pod_namespace` | Multiple pods, one namespace | 3 | 1 | 1.5 cores | 3 GiB |
| `multi_namespace` | Workloads across namespaces | 2 | 2 | 1.5 cores | 3 GiB |
| `high_utilization` | High resource usage | 1 | 1 | 2.0 cores | 4 GiB |

**Key Implementation Detail**: Dates are **dynamically injected** into YAML templates at runtime. This solves the "hardcoded dates" problem that causes Koku to reject data with "missing start or end dates" errors.

**Example YAML Template** (dates replaced at runtime):
```yaml
generators:
  - OCPGenerator:
      start_date: {start_date}  # Injected dynamically
      end_date: {end_date}      # Injected dynamically
      nodes:
        - node:
          node_name: test-node-1
          cpu_cores: 2
          memory_gig: 8
          namespaces:
            test-namespace:
              pods:
                - pod:
                  pod_name: test-pod-1
                  cpu_request: 0.5
                  mem_request_gig: 1
```

**Markers**: `@pytest.mark.e2e`, `@pytest.mark.scenario`

---

## Using Scenarios with E2E Tests

To use a custom scenario with the E2E data flow tests:

```python
from test_scenarios import generate_scenario_yaml, get_scenario_expected_values

# Generate YAML with current dates
yaml_path = generate_scenario_yaml(
    "multi_pod_namespace",
    start_date=datetime.utcnow() - timedelta(days=1),
    end_date=datetime.utcnow(),
    output_dir="/tmp/nise",
)

# Get expected values for validation
expected = get_scenario_expected_values("multi_pod_namespace", hours=24)
# expected = {
#     "pod_count": 3,
#     "namespace_count": 1,
#     "expected_cpu_hours": 36.0,  # 1.5 cores * 24 hours
#     "expected_memory_gb_hours": 72.0,  # 3 GiB * 24 hours
# }
```

Or via environment variable:
```bash
E2E_NISE_STATIC_REPORT=/path/to/scenario.yml ./scripts/run-pytest.sh --e2e
```

---

## Running E2E Tests

```bash
# Run scenario validation tests (fast, no cluster needed)
pytest tests/suites/e2e/test_scenarios.py -v -m scenario

# Run full E2E data flow tests
./scripts/run-pytest.sh --e2e

# Run E2E smoke tests only
./scripts/run-pytest.sh -- -m "e2e and smoke"
```

## VM tests: default CI vs extended

| Suite | Path | Marker | When to run |
|-------|------|--------|-------------|
| VM smoke (API contract) | `tests/suites/ros/test_vm_smoke.py` | `@pytest.mark.smoke` | Every PR (`./scripts/run-pytest.sh --ros`) |
| VM list/detail (basic) | `tests/suites/ros/test_vm_recommendations.py` | `@pytest.mark.integration` | Every PR |
| VM settings | `tests/suites/ros/test_vm_settings.py` | integration | Every PR |
| VM notifications matrix | `tests/suites/e2e/test_vm_notifications_matrix.py` | `@pytest.mark.extended` | Weekly / before release |
| VM GPU flow | `tests/suites/e2e/test_vm_gpu_flow.py` | extended | Weekly |
| VM preference flow | `tests/suites/e2e/test_vm_preference_flow.py` | extended | Weekly |
| VM recommendations flow | `tests/suites/e2e/test_vm_recommendations_flow.py` | extended | Weekly |
| VM instance types | `tests/suites/e2e/test_vm_instance_types_flow.py` | extended | Weekly |
| VM enhancements | `tests/suites/e2e/test_vm_enhancements_flow.py` | extended | Weekly |
| VM MVP promotions | `tests/suites/e2e/test_vm_mvp_promotions_flow.py` | extended | Weekly |

**Recommendation:** Run default CI on every PR; schedule
`NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm` at least weekly (or before
on-prem releases) so notification codes, dual-engine divergence, GPU device CSVs, and
`cluster_instance_types.json` preference ingest stay validated.

Smoke tests (`test_vm_smoke.py`) catch regressions such as removed filters
(`filter[has_gpu]`, `filter[engine]`, `filter[confidence]`) or broken history pagination
without requiring NISE payloads for each scenario.

---

## VM MVP promotions flow (`test_vm_mvp_promotions_flow.py`)

Extended E2E for phase-11 VM promotion scenarios: adaptive CPU margin (`variable-cpu-vm`),
`current_instance_type` on u1-shaped VMs, GPU time-slicing and MIG profile recommendations,
multi-GPU notification **54**, recommendation history API, and per-device `gpu_devices` detail
after `ocp_ros_vm_gpu_device` ingest. Uses NISE template `ocp_report_vm_mvp_promotions.yml`.

```bash
NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_mvp_promotions_flow
```

---

## Namespace recommendations flow (`test_namespace_recommendations_flow.py`)

Extended E2E that validates the namespace recommendation pipeline end-to-end:
source registration → NISE data generation (with `--ros-ocp-info`) → upload →
Koku processing → ROS ingestion → namespace recommendations available via
`/recommendations/openshift/namespaces`. Verifies both `all_hours` and
`business_hours` schedule types when business hours are configured.

```bash
NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k namespace_recommendations_flow
```

---

## Related Files

- `test_complete_flow.py` - Full E2E data flow tests (source → upload → processing → recommendations)
- `test_namespace_recommendations_flow.py` - Namespace recommendation pipeline E2E
- `test_vm_mvp_promotions_flow.py` - VM MVP promotion and multi-GPU device detail E2E
- `test_smoke.py` - Quick smoke tests for E2E validation
- `conftest.py` - Shared fixtures for E2E tests
- `../../e2e_helpers.py` - Centralized E2E helpers (NISE config, source registration, upload utilities)