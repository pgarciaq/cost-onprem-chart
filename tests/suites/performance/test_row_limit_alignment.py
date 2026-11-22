#
# Copyright 2026 Red Hat Inc.
# SPDX-License-Identifier: Apache-2.0
"""Row-limit alignment for the scale-bench fixture (#894).

nise flushes a report file when its row accumulator hits ``--file-row-limit``
(``nise/report.py``), purely by row count. The ROS usage generator emits
interval-major — ``for quarter_hour: for pod: yield`` — so a limit that is
not a multiple of the cluster-hour block (4 quarter-hours x per-interval
rows) splits mid-hour, and the processor's (org, cluster, node, date, hour)
upsert keeps only the last file's subset for the boundary hour.

These tests pin the generator's alignment arithmetic and the post-generation
scan that enforces it.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CHART_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = CHART_ROOT / "scripts" / "generate-scale-bench-data.py"

_spec = importlib.util.spec_from_file_location("generate_scale_bench_data", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_scale_bench_data"] = gen
_spec.loader.exec_module(gen)


@pytest.mark.parametrize(
    "pod_rows,ns_rows",
    [
        (1242, 126),  # bench-v3 shape: 9 nodes x 138 pods, 9x14 namespaces
        (4000, 500),  # medium profile shape: 25 nodes x 160 pods, 500 ns
        (750, 150),  # small profile shape: 15 nodes x 50 pods, 150 ns
        (7, 3),  # tiny heterogeneous: lcm matters
        (160, 160),  # single namespace per pod-row (ns == pods)
    ],
)
def test_aligned_row_limit_is_a_cluster_hour_multiple(pod_rows, ns_rows):
    """The limit must be a whole number of cluster-hours for any shape."""
    hour_block = gen.alignment_hour_block(pod_rows, ns_rows)
    limit = gen.aligned_row_limit(hour_block)
    assert limit % hour_block == 0
    assert limit >= hour_block
    # close to nise's 100k default, so the file count stays comparable
    assert limit <= 100_000 + hour_block


def test_the_hour_block_is_the_lcm_of_both_report_types():
    """Container rows (pods) and namespace rows must align simultaneously."""
    # 7 pod-rows and 3 ns-rows per interval: the hour block must be a
    # multiple of both 4x7 and 4x3, i.e. 4 x lcm(7, 3) = 84.
    assert gen.alignment_hour_block(7, 3) == 84
    # uniform shape: pods = ns x k, lcm = pods
    assert gen.alignment_hour_block(4000, 500) == 4 * 4000


def test_the_original_issue_arithmetic_would_not_align():
    """k x pods_per_node x 4 x 24 is NOT an hour-block multiple (#894).

    The issue's proposal assumed node-major emission; nise emits
    interval-major, so at the bench-v3 shape (1242 pods across 9 nodes)
    the proposed 7 x 13,248 = 92,736 is not a multiple of the 4,968-row
    cluster-hour and would keep splitting mid-hour. Pinned so nobody
    "simplifies" the arithmetic back to the broken form.
    """
    pods_per_node = 138
    nodes = 9
    hour_block = gen.alignment_hour_block(pods_per_node * nodes, 126)
    issue_proposal = 7 * pods_per_node * 4 * 24
    assert issue_proposal % hour_block != 0, (
        "if this starts passing, the emission order changed — revisit"
    )


def test_nise_command_carries_the_aligned_limit():
    """The invocation must pass --file-row-limit (the default 100k misaligns)."""
    cmd = gen.nise_command("/bin/nise", Path("/tmp/x.yml"), "uuid", 98765)
    assert "--file-row-limit" in cmd
    assert cmd[cmd.index("--file-row-limit") + 1] == "98765"
    assert "--ros-ocp-info" in cmd and "-w" in cmd


def _write_csv(path, rows):
    import csv

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["interval_start", "node", "namespace"])
        writer.writeheader()
        writer.writerows(rows)


def test_scan_flags_a_partial_hour(tmp_path):
    """The boundary-hour failure mode: an hour with 2 of 4 intervals.

    interval_start uses nise's real Go format (``2026-11-01 06:15:01
    +0000 UTC``) — the first scan implementation parsed only ISO and
    silently skipped every real row, which is exactly how a scan becomes
    vacuous.
    """
    rows = []
    for h in (1, 2):
        for q in (0, 15, 30, 45):
            rows.append(
                {
                    "interval_start": f"2026-11-01 0{h}:{q:02d}:01 +0000 UTC",
                    "node": "n1",
                    "namespace": "ns1",
                }
            )
    # hour 3: only 2 of 4 quarter-hours — the split victim
    for q in (0, 15):
        rows.append(
            {
                "interval_start": f"2026-11-01 03:{q:02d}:01 +0000 UTC",
                "node": "n1",
                "namespace": "ns1",
            }
        )
    csv_path = tmp_path / "bench_ocp_ros_usage_1.csv"
    _write_csv(csv_path, rows)
    violations = gen.scan_hour_completeness([csv_path], "node")
    assert len(violations) == 1
    assert "n1" in violations[0] and "2/4" in violations[0]


def test_scan_accepts_both_timestamp_spellings(tmp_path):
    """ISO and Go spellings both parse — neither may silently no-op."""
    rows = [
        {"interval_start": "2026-11-01T01:00:00Z", "node": "n1", "namespace": "ns1"},
        {"interval_start": "2026-11-01 01:15:01 +0000 UTC", "node": "n1", "namespace": "ns1"},
        {"interval_start": "2026-11-01 01:30:01 +0000 UTC", "node": "n1", "namespace": "ns1"},
        {"interval_start": "2026-11-01 01:45:01 +0000 UTC", "node": "n1", "namespace": "ns1"},
    ]
    csv_path = tmp_path / "bench_ocp_ros_usage_1.csv"
    _write_csv(csv_path, rows)
    assert gen.scan_hour_completeness([csv_path], "node") == []


def test_scan_passes_complete_hours(tmp_path):
    rows = []
    for h in range(3):
        for q in (0, 15, 30, 45):
            rows.append(
                {
                    "interval_start": f"2026-11-01 0{h}:{q:02d}:01 +0000 UTC",
                    "node": "n1",
                    "namespace": "ns1",
                }
            )
    csv_path = tmp_path / "bench_ocp_ros_usage_1.csv"
    _write_csv(csv_path, rows)
    assert gen.scan_hour_completeness([csv_path], "node") == []


def test_interval_rows_from_yaml_counts_gpu_variants():
    """GPU pods yield one row per gpu_uuid — the variant count must ride along."""
    cfg = {
        "generators": [
            {
                "OCPGenerator": {
                    "nodes": [
                        {
                            "node": {
                                "namespaces": {
                                    "ns-a": {
                                        "pods": [
                                            {"pod": {"gpus": [{"gpu_uuid": "1"}, {"gpu_uuid": "2"}]}},
                                            {"pod": {}},
                                        ]
                                    },
                                    "ns-b": {"pods": [{"pod": {}}]},
                                }
                            }
                        }
                    ]
                }
            }
        ]
    }
    pod_rows, ns_rows = gen.interval_rows_from_yaml(cfg)
    assert pod_rows == 4  # 2 (gpu variants) + 1 + 1
    assert ns_rows == 2
