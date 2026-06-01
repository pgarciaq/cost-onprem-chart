# NISE Templates

**TEMPORARY**: These templates are copied from `iqe-cost-management-plugin` pending full IQE test integration.

Once IQE tests are integrated, these templates should be removed and the test framework should reference the IQE plugin's templates directly.

## Source

Templates copied from:
```
iqe-cost-management-plugin/iqe_cost_management/data/openshift/
```

## Templates

| Template | Purpose |
|----------|---------|
| `ocp_report_business_hours.yml` | Business Hours E2E — 3 nodes, 3 namespaces (daytime/batch/nighttime workloads) with `--ros-ocp-info` |
| `ocp_report_quota.yml` | Quota recommendation E2E — high pod requests + `resource_quota` used columns for ROS quota plugin |
| `ocp_report_vm.yml` | VM recommendation E2E — `OCPVirtualMachineGenerator` (guest agent, idle, Linux/Windows) |
| `ocp_report_vm_gpu.yml` | VM GPU E2E — GPU columns, classifications 50–53, `filter[has_gpu]`, gn1 matching |
| `ocp_report_vm_io_profiling.yml` | VM disk I/O pattern E2E — sequential/random classification, notifications 58–59 |
| `ocp_report_vm_enhancements.yml` | VM MVP enhancements — notifications 46–49, kernel reserve, gn1 catalog |
| `ocp_report_ros_0.yml` | ROS optimization testing - multiple nodes with varied CPU/memory usage patterns |
| `ocp_report_advanced.yml` | Complex OCP scenario with multiple nodes, namespaces, volumes, and labels |
| `ocp_report_advanced_daily.yml` | Same as advanced but with daily data granularity |
| `ocp_report_distro.yml` | Distribution testing across multiple configurations |
| `ocp_ai_workloads_template.yml` | AI/ML workload patterns |
| `ocp_report_0_template.yml` | Basic template for customization |
| `ocp_report_1.yml` | Simple single-pod report |
| `ocp_report_2.yml` | Two-pod report |
| `ocp_report_7.yml` | Seven-day report pattern |
| `ocp_report_daily_flow_template.yml` | Daily data flow testing |
| `ocp_report_forecast_const.yml` | Constant usage for forecast testing |
| `ocp_report_forecast_outlier.yml` | Outlier patterns for forecast testing |
| `ocp_report_missing_items.yml` | Testing missing data handling |
| `ocp_random_cpu_for_eap_report.yml` | Random CPU patterns for EAP |
| `today_ocp_report_*.yml` | Various "today" date-based templates |

## Usage

Set the `NISE_IQE_TEMPLATE` environment variable when running tests:

```bash
# Use Business Hours E2E template
NISE_IQE_TEMPLATE=ocp_report_business_hours.yml pytest -v suites/ros/test_business_hours.py

# Use ROS optimization template
NISE_IQE_TEMPLATE=ocp_report_ros_0.yml pytest -v suites/ui/test_data_validation.py

# Use advanced multi-node template
NISE_IQE_TEMPLATE=ocp_report_advanced.yml pytest -v suites/cost_management/
```

Without the env var, tests use the default `NISEConfig` which generates a simple single-node, single-pod setup.

## Template Format

Templates use NISE YAML format with date placeholders:
- `start_date: last_month` - Replaced with calculated start date
- `start_date: today` - Replaced with test run date

The test framework automatically substitutes these placeholders with actual dates.
