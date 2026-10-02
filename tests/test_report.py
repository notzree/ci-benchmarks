import copy
import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report.py"
SPEC = importlib.util.spec_from_file_location("report", SCRIPT)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def result(workload="vue", run_id="1"):
    phases = {name: {"wall_seconds": 2.0, "user_seconds": 1.0, "system_seconds": 0.5,
                     "max_rss_kib": 100, "exit_code": 0}
              for name in ("setup", "checkout", "install", "build", "probe", "total")}
    phases["total"] = {"wall_seconds": 12.0, "exit_code": 0}
    if workload == "vue":
        phases["test"] = copy.deepcopy(phases["build"])
    return {"schema_version": 1, "provider": "github", "workload": workload,
            "scenario": "cold", "series": "one", "trial": 1, "seed_id": None,
            "run_id": run_id, "run_attempt": 1, "commit": "abc", "upstream_sha": "def",
            "fingerprint": "fp", "toolchains": {"node": "22"}, "outcome": "success",
            "cache": {}, "system": {"architecture": "x86_64", "logical_cpus": 4,
                                    "cpu_model": "reported CPU", "memory_limit_bytes": 1024,
                                    "cpu_quota": 4}, "phases": phases}


def metadata(value):
    return {**{key: value[key] for key in report.IDENTITY}, "head_sha": value["commit"],
            "status": "completed", "conclusion": "success",
            "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:00Z",
            "jobs": [
                {"name": "GitHub-hosted benchmark", "started_at": "2026-01-01T00:00:05Z",
                 "completed_at": "2026-01-01T00:00:55Z", "steps": [
                     {"name": "Restore immutable seed", "started_at": "2026-01-01T00:00:06Z",
                      "completed_at": "2026-01-01T00:00:09Z"},
                     {"name": "Save immutable seed", "started_at": "2026-01-01T00:00:50Z",
                      "completed_at": "2026-01-01T00:00:54Z"}]},
                {"name": "Watchdog", "started_at": "2026-01-01T00:00:00Z",
                 "completed_at": "2026-01-01T00:01:00Z"}]}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.output = self.root / "output"

    def put(self, relative, value):
        path = self.results / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def trial(self, value=None, meta=None, directory=None):
        value = value or result()
        directory = directory or f"runs/{value['run_id']}-{value['run_attempt']}"
        self.put(f"{directory}/metadata.json", metadata(value) if meta is None else meta)
        return self.put(f"{directory}/artifact/result.json", value)

    def collect(self):
        return report.collect(self.results)[0]

    def test_even_odd_median_p95_population_stddev(self):
        self.assertEqual(report.statistics_for([4, 1, 3, 2])["median"], 2.5)
        stats = report.statistics_for([3, 1, 2])
        self.assertEqual(stats["median"], 2)
        self.assertEqual(stats["p95"], 3)
        self.assertAlmostEqual(stats["population_stddev"], (2 / 3) ** 0.5)
        self.assertEqual(report.statistics_for(range(1, 21))["p95"], 19)
        self.assertEqual(report.statistics_for([])["n"], 0)

    def test_hugo_optional_test_vue_required(self):
        self.trial(result("hugo"))
        vue = result(run_id="2")
        del vue["phases"]["test"]
        self.trial(vue)
        records = self.collect()
        self.assertTrue(records[0]["trial_success"])
        self.assertFalse(records[1]["trial_success"])
        self.assertIn("missing required phase timing: test", records[1]["reasons"])

    def test_failed_phase_outcome_and_cleanup_visible_excluded(self):
        self.trial()
        failed = result(run_id="2")
        failed["phases"]["build"]["exit_code"] = 1
        failed["outcome"] = "failure"
        self.trial(failed)
        cleanup = result(run_id="3")
        meta = metadata(cleanup)
        meta["conclusion"] = "failure"
        self.trial(cleanup, meta)
        records = self.collect()
        self.assertTrue(records[2]["workload_success"])
        self.assertFalse(records[2]["trial_success"])
        group = report.summarize(records, [])["groups"][0]
        self.assertEqual(group["n_success"], 1)
        self.assertEqual(group["n_failure"], 2)
        self.assertEqual(group["metrics"]["total.wall_seconds"]["n"], 1)

    def test_collection_error_overrides_success(self):
        value = result()
        meta = metadata(value)
        meta["collection_error"] = "download failed"
        self.trial(value, meta)
        self.assertFalse(self.collect()[0]["trial_success"])

    def test_invalid_metrics_fail(self):
        for bad in (-1, float("nan"), float("inf"), "3", True, None):
            with self.subTest(bad=bad):
                value = result()
                value["phases"]["build"]["wall_seconds"] = bad
                with self.assertRaises(ValueError):
                    report.validate_result(value, "test")

    def test_missing_exit_and_total_incomplete(self):
        value = result()
        del value["phases"]["setup"]["exit_code"]
        del value["phases"]["total"]
        self.trial(value)
        self.assertFalse(self.collect()[0]["workload_success"])

    def test_heterogeneous_grouping_seed_separation(self):
        values = [result(run_id=str(index)) for index in range(9)]
        values[1]["system"]["cpu_model"] = "other CPU"
        values[2]["system"]["cpu_quota"] = 2
        values[3]["fingerprint"] = "different"
        values[4]["toolchains"] = {"node": "24"}
        values[5]["seed_id"] = "seed-A"
        values[6]["scenario"] = "seed"
        values[7]["commit"] = "another"
        values[8]["upstream_sha"] = "another"
        for value in values:
            self.trial(value)
        self.assertEqual(len(report.summarize(self.collect(), [])["groups"]), 9)

    def test_byte_equivalent_duplicates_and_conflicts(self):
        value = result()
        self.trial(value)
        self.trial(value, directory="copies/one")
        self.assertEqual(len(self.collect()), 1)
        value["phases"]["total"]["wall_seconds"] = 13
        self.trial(value, directory="copies/two")
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.collect()

    def test_identity_commit_mismatch(self):
        for key in (*report.IDENTITY, "head_sha"):
            with self.subTest(key=key):
                value = result()
                meta = metadata(value)
                meta[key] = "different"
                self.trial(value, meta)
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    self.collect()

    def test_elapsed_jobs_cache_parallel_watchdog(self):
        self.trial()
        metrics = self.collect()[0]["metrics"]
        self.assertEqual(metrics["workflow.wall_seconds"], 60)
        self.assertEqual(metrics["benchmark_job.wall_seconds"], 50)
        self.assertEqual(metrics["cache_restore.wall_seconds"], 3)
        self.assertEqual(metrics["cache_save.wall_seconds"], 4)
        with self.assertRaises(ValueError):
            report.elapsed("2026-01-01T00:01:00Z", "2026-01-01T00:00:00Z", "bad")

    def test_native_provision_separate_from_workload(self):
        value = result()
        value["provider"] = "archil"
        meta = metadata(value)
        meta["jobs"][0]["name"] = "Archil native benchmark"
        self.trial(value, meta)
        self.put("runs/1-1/native/native-provision.json",
                 {"totalProvisionSeconds": 8, "timings": {"forkRunner": 2, "startRunner": 6}})
        metrics = self.collect()[0]["metrics"]
        self.assertEqual(metrics["provision.total_seconds"], 8)
        self.assertEqual(metrics["provision.forkRunner.wall_seconds"], 2)
        self.assertEqual(metrics["total.wall_seconds"], 12)
        self.assertNotIn("total.user_seconds", metrics)

    def test_missing_artifacts_suite_skipped_no_success_cli(self):
        value = result()
        meta = metadata(value)
        meta["conclusion"] = "failure"
        self.put("runs/1-1/metadata.json", meta)
        skipped = {**{key: value[key] for key in report.IDENTITY},
                   "run_id": None, "trial": 2, "status": "skipped"}
        self.put("suite.json", {"trials": [skipped]})
        completed = subprocess.run([sys.executable, str(SCRIPT), str(self.results),
                                    "--output", str(self.output)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["n_success"], 0)
        self.assertEqual(summary["n_failure"], 2)
        self.assertTrue(all(not group["metrics"] for group in summary["groups"]))
        markdown = (self.output / "summary.md").read_text()
        self.assertIn("No successful timing samples", markdown)
        self.assertIn("suite trial skipped", markdown)
        self.assertIn("missing workload result", markdown)
        self.assertTrue((self.output / "summary.csv").exists())
        self.assertTrue((self.output / "trials.csv").exists())

    def test_missing_metadata_excludes_workload(self):
        self.put("artifact/result.json", result())
        record = self.collect()[0]
        self.assertTrue(record["workload_success"])
        self.assertFalse(record["trial_success"])

    def test_csv_injection_unicode(self):
        value = result()
        value["series"] = " =HYPERLINK(\"bad\")"
        value["system"]["cpu_model"] = "CPU – reported"
        self.trial(value)
        report.write_report(self.results, self.output)
        with (self.output / "trials.csv").open(newline="", encoding="utf-8") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["series"][0], "'")
        self.assertEqual(json.loads(row["hardware"])["cpu_model"], "CPU – reported")
        for text in ("=x", "+x", "-x", "@x", "\t=x", "\n=x", "\ttext"):
            self.assertTrue(report.csv_value(text).startswith("'"))

    def test_empty_results_cli_validation_error(self):
        self.assertEqual(report.write_report(self.results, self.output)["n_trials"], 0)
        value = result()
        value["schema_version"] = 2
        self.put("artifact/result.json", value)
        completed = subprocess.run([sys.executable, str(SCRIPT), str(self.results),
                                    "--output", str(self.output)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unsupported schema_version", completed.stderr)


if __name__ == "__main__":
    unittest.main()
