import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "suite", Path(__file__).resolve().parents[1] / "scripts" / "suite.py")
suite = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(suite)
COMMIT = "a" * 40
TEMPLATE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def time(self):
        return 1700000000 + self.now

    def sleep(self, seconds):
        self.now += seconds


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as output:
        for name, value in files.items():
            output.writestr(name, value if isinstance(value, str) else json.dumps(value))
    return data.getvalue()


class FakeGhProcess:
    """Fake only the gh transport: tests execute the production coordinator."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.dispatches = []
        self.runs = {}
        self.inputs = {}
        self.cancelled = []
        self.run_hook = lambda run, inputs: None
        self.receipt_hook = lambda receipt: None
        self.list_hook = lambda runs: runs
        self.attempt_hook = lambda run: run
        self.fail_endpoint = None
        self.missing_receipt = False
        self.missing_bench = False
        self.missing_provision = False
        self.download_failure = False
        self.workflow_path = ".github/workflows/benchmark.yml"

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        endpoint = command[2]
        method = command[4]
        if self.fail_endpoint and self.fail_endpoint in endpoint:
            return subprocess.CompletedProcess(command, 1, b"", b"API unavailable")
        payload = json.loads(kwargs["input"]) if kwargs["input"] else None
        value = self.respond(endpoint, method, payload)
        output = value if isinstance(value, bytes) else json.dumps(value).encode() if value else b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    def respond(self, endpoint, method, payload):
        if "/commits/" in endpoint:
            return {"sha": COMMIT}
        if endpoint.endswith("/actions/workflows/benchmark.yml"):
            return {"id": 7, "path": self.workflow_path}
        if endpoint.endswith("/dispatches"):
            self.dispatches.append(payload)
            inputs = payload["inputs"]
            run_id = 100 + len(self.dispatches)
            stamp = suite.dt.datetime.fromtimestamp(
                self.clock.time(), suite.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            run = {
                "id": run_id, "run_attempt": 1, "workflow_id": 7,
                "event": "workflow_dispatch", "head_branch": payload["ref"],
                "head_sha": COMMIT, "created_at": stamp, "updated_at": stamp,
                "run_started_at": stamp, "status": "completed", "conclusion": "success",
                "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
                "display_title": "ci-bench/{provider}/{workload}/{scenario}/{series}/{trial}".format(
                    **inputs),
            }
            self.run_hook(run, inputs)
            self.runs[run_id], self.inputs[run_id] = run, inputs
            return None
        if "/actions/workflows/7/runs?" in endpoint:
            return {"workflow_runs": self.list_hook(list(self.runs.values()))}
        if "/actions/artifacts/" in endpoint:
            artifact_id = int(endpoint.split("/artifacts/")[1].split("/")[0])
            run_id, provision = divmod(artifact_id, 10)
            if self.download_failure:
                raise suite.SuiteError("download unavailable")
            if provision:
                return archive({"controller.json": {"runner": "test"}})
            inputs = self.inputs[run_id]
            files = {
                "result.json": {"schema_version": 1, "status": "success"},
                "result.csv": "phase,seconds\nbuild,1\n",
                "logs/build.log": "built\n",
            }
            if inputs["scenario"] == "seed" and not self.missing_receipt:
                receipt = {
                    "schema_version": 1, "repository": "example/project",
                    "provider": inputs["provider"], "workload": inputs["workload"],
                    "series": inputs["series"], "seed_id": inputs["seed-id"],
                    "run_id": run_id, "run_attempt": 1, "commit": COMMIT,
                    "template_id": TEMPLATE if inputs["provider"] == "archil" else "",
                    "cache_key": (
                        f"ci-bench-v1-Linux-X64-{inputs['workload']}-{inputs['seed-id']}-{run_id}-1"
                        if inputs["provider"] == "github" else ""),
                }
                self.receipt_hook(receipt)
                files["seed.json"] = receipt
            return archive(files)
        if "/actions/runs/" in endpoint:
            run_id = int(endpoint.split("/runs/")[1].split("/")[0].split("?")[0])
            run = self.runs[run_id]
            if "/jobs?" in endpoint:
                return {"jobs": [{
                    "name": "benchmark", "started_at": run["run_started_at"],
                    "completed_at": run["updated_at"], "conclusion": run["conclusion"],
                    "steps": [{"name": "build", "conclusion": "success"}],
                }]}
            if "/artifacts?" in endpoint:
                artifacts = []
                if not self.missing_bench:
                    artifacts.append({"id": run_id * 10, "name": f"bench-{run_id}-1"})
                if self.inputs[run_id]["provider"] == "archil" and not self.missing_provision:
                    artifacts.append({"id": run_id * 10 + 1, "name": f"provision-{run_id}-1"})
                # An artifact for another attempt must never satisfy this one.
                artifacts.append({"id": 9999, "name": f"bench-{run_id}-2"})
                return {"artifacts": artifacts}
            if endpoint.endswith("/cancel"):
                self.cancelled.append(run_id)
                return None
            if "/attempts/" in endpoint:
                return self.attempt_hook(dict(run))
            return run
        raise AssertionError(f"unexpected request: {method} {endpoint}")


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.clock = FakeClock()
        self.process = FakeGhProcess(self.clock)

    def args(self, *extra):
        return suite.parse_args([
            "--repository", "example/project", "--ref", "feature/test",
            "--output", str(self.output), "--series", "test-series",
            "--trials", "2", *extra,
        ])

    def coordinator(self, *extra, **options):
        return suite.Coordinator(self.args(*extra), suite.Gh(self.clock, self.process),
                                 self.clock, poll_seconds=1, **options)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def test_fresh_runs_order_pinned_sha_and_outputs(self):
        coordinator = self.coordinator()
        self.assertEqual(coordinator.run(), 0)
        dispatches = self.process.dispatches
        self.assertEqual(len(dispatches), 20)
        self.assertEqual([p["inputs"]["scenario"] for p in dispatches],
                         ["cold"] * 8 + ["seed"] * 4 + ["warm"] * 8)
        self.assertEqual([p["inputs"]["provider"] for p in dispatches[:8]],
                         ["github", "archil"] * 2 + ["archil", "github"] * 2)
        self.assertEqual([p["inputs"]["provider"] for p in dispatches[-8:]],
                         ["github", "archil"] * 2 + ["archil", "github"] * 2)
        self.assertTrue(all(p["ref"] == "feature/test" for p in dispatches))
        self.assertEqual(len(set(r["display_title"] for r in self.process.runs.values())), 20)
        self.assertEqual(sum("/commits/" in call[0][2] for call in self.process.calls), 1)
        seeds = self.read("seeds.json")["seeds"]
        self.assertEqual(len(seeds), 4)
        for payload in dispatches:
            inputs = payload["inputs"]
            if inputs["scenario"] == "warm":
                seed = next(s for s in seeds if s["seed_id"] == inputs["seed-id"])
                if inputs["provider"] == "github":
                    self.assertEqual(inputs["cache-key"], seed["cache_key"])
                    self.assertEqual(inputs["template-id"], "")
                else:
                    self.assertEqual(inputs["template-id"], TEMPLATE)
                    self.assertEqual(inputs["cache-key"], "")
            else:
                self.assertEqual(inputs["cache-key"], "")
                self.assertEqual(inputs["template-id"], "")
        self.assertEqual(self.read("suite.json")["commit"], COMMIT)
        metadata = self.read("runs/101-1/metadata.json")
        self.assertEqual(metadata["head_sha"], COMMIT)
        self.assertEqual(metadata["provider"], "github")
        self.assertEqual(metadata["jobs"][0]["steps"][0]["name"], "build")
        self.assertTrue((self.output / "runs/102-1/provision/controller.json").exists())
        self.assertTrue((self.output / "runs/101-1/bench/logs/build.log").exists())

    def test_wrong_dispatch_matches_are_ignored(self):
        def distract(runs):
            run = dict(runs[-1])
            extras = []
            for key, value in [
                ("display_title", "someone else's run"), ("workflow_id", 99),
                ("event", "push"), ("head_branch", "other"), ("created_at", "2000-01-01T00:00:00Z"),
            ]:
                extras.append(dict(run, **{key: value, "id": 999}))
            return extras + runs
        self.process.list_hook = distract
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 0)

    def test_duplicate_dispatches_fail_without_selecting_one(self):
        self.process.list_hook = lambda runs: runs + [dict(runs[-1], id=999)]
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        entry = self.read("suite.json")["trials"][0]
        self.assertIn("multiple exact", entry["error"])
        self.assertNotIn("run_id", entry)
        self.assertEqual(entry["discovery_matches"], [101, 999])

    def test_moved_branch_rejected_and_metadata_visible(self):
        def moved(run, inputs):
            if inputs["scenario"] == "cold":
                run["head_sha"] = "b" * 40
        self.process.run_hook = moved
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        metadata = self.read("runs/101-1/metadata.json")
        self.assertIn("ref may have moved", metadata["collection_error"])
        self.assertEqual(metadata["head_sha"], "b" * 40)

    def test_attempt_specific_polling_and_unexpected_attempt(self):
        self.process.run_hook = lambda run, inputs: run.update(status="in_progress", conclusion=None)
        self.process.attempt_hook = lambda run: dict(run, run_attempt=2)
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        endpoints = [c[0][2] for c in self.process.calls]
        self.assertTrue(any("/attempts/1" in endpoint for endpoint in endpoints))
        self.assertIn("unexpected rerun", self.read("suite.json")["trials"][0]["error"])

    def test_failed_seed_skips_warm_but_independent_provider_continues(self):
        def fail(run, inputs):
            if inputs["scenario"] == "seed" and inputs["provider"] == "github":
                run["conclusion"] = "failure"
        self.process.run_hook = fail
        self.assertEqual(self.coordinator("--workloads", "vue").run(), 1)
        seeds = self.read("seeds.json")["seeds"]
        self.assertEqual([s["provider"] for s in seeds], ["archil"])
        entries = self.read("suite.json")["trials"]
        self.assertEqual([e["state"] for e in entries
                          if e["scenario"] == "warm" and e["provider"] == "github"],
                         ["skipped", "skipped"])
        self.assertTrue(all(e["state"] == "completed" for e in entries
                            if e["scenario"] == "warm" and e["provider"] == "archil"))

    def test_missing_and_mismatched_receipt_never_falls_back_cold(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.output = Path(self.temp.name) / str(missing)
                self.process = FakeGhProcess(self.clock)
                self.process.missing_receipt = missing
                self.process.receipt_hook = lambda receipt: receipt.update(commit="b" * 40)
                self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
                self.assertEqual(self.read("seeds.json")["seeds"], [])
                self.assertFalse(any(p["inputs"]["scenario"] == "warm"
                                     for p in self.process.dispatches))
                self.assertEqual(self.read("suite.json")["trials"][-1]["state"], "skipped")

    def test_seed_receipt_contract_and_path_safety(self):
        coordinator = self.coordinator("--workloads", "vue", "--providers", "github")
        self.assertEqual(coordinator.run(), 0)
        receipt = self.read("seeds.json")["seeds"][0]
        entry = next(e for e in coordinator.suite["trials"] if e["scenario"] == "seed")
        changes = {
            "schema_version": True, "repository": "other/repo", "provider": "archil",
            "workload": "hugo", "series": "../evil", "seed_id": "../evil",
            "run_id": str(receipt["run_id"]), "run_attempt": 2,
            "commit": "b" * 40, "cache_key": receipt["cache_key"] + "/../../evil",
        }
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(suite.SuiteError):
                suite.validate_seed(dict(receipt, **{key: value}), "example/project", entry, COMMIT)
        archil = dict(entry, provider="archil")
        for template in ("", "../../evil", "not-a-uuid"):
            with self.subTest(template=template), self.assertRaises(suite.SuiteError):
                suite.validate_seed(dict(receipt, provider="archil", template_id=template),
                                    "example/project", archil, COMMIT)

    def test_seed_persisted_even_if_other_collection_fails(self):
        self.process.fail_endpoint = "/jobs?"
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        self.assertEqual(len(self.read("seeds.json")["seeds"]), 1)
        self.assertTrue(any(p["inputs"]["scenario"] == "warm" for p in self.process.dispatches))
        self.assertIn("API unavailable", self.read("runs/101-1/metadata.json")["collection_error"])

    def test_download_or_missing_artifact_does_not_look_successful(self):
        for mode in ("download_failure", "missing_bench", "missing_provision"):
            with self.subTest(mode=mode):
                self.output = Path(self.temp.name) / mode
                self.process = FakeGhProcess(self.clock)
                setattr(self.process, mode, True)
                self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "archil").run(), 1)
                self.assertEqual(self.read("suite.json")["status"], "failed")
                self.assertIn("collection_error", self.read("runs/101-1/metadata.json"))

    def test_bounded_discovery_queue_runtime_and_total_budget(self):
        for mode in ("discovery", "queue", "runtime", "total"):
            with self.subTest(mode=mode):
                self.output = Path(self.temp.name) / mode
                self.clock = FakeClock()
                self.process = FakeGhProcess(self.clock)
                if mode == "discovery":
                    self.process.list_hook = lambda runs: []
                else:
                    status = "queued" if mode == "queue" else "in_progress"
                    self.process.run_hook = lambda run, inputs: run.update(
                        status=status, conclusion=None)
                options = {"discovery_seconds": 3, "queue_seconds": 3,
                           "runtime_seconds": 3, "total_seconds": 2 if mode == "total" else 100}
                self.assertEqual(self.coordinator(**options).run(), 130)
                self.assertEqual(len(self.process.dispatches), 1)
                self.assertLessEqual(self.clock.now, 14)
                self.assertEqual(self.read("suite.json")["status"], "interrupted")
                if mode != "discovery":
                    self.assertTrue(self.process.cancelled)

    def test_poll_api_failure_cancels_and_records_error(self):
        self.process.run_hook = lambda run, inputs: run.update(status="in_progress", conclusion=None)
        self.process.fail_endpoint = "/attempts/1"
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        self.assertTrue(self.process.cancelled)
        self.assertIn("API unavailable", self.read("runs/101-1/metadata.json")["collection_error"])

    def test_interrupt_cancels_exact_active_child_no_more_dispatches(self):
        self.process.run_hook = lambda run, inputs: run.update(status="in_progress", conclusion=None)
        def interrupt(run):
            raise KeyboardInterrupt()
        self.process.attempt_hook = interrupt
        self.assertEqual(self.coordinator().run(), 130)
        self.assertEqual(self.process.cancelled, [101])
        self.assertEqual(len(self.process.dispatches), 1)
        entries = self.read("suite.json")["trials"]
        self.assertEqual(entries[0]["state"], "failed")
        self.assertEqual(entries[0]["status"], "failed")
        self.assertFalse(entries[0]["unresolved_dispatch"])
        self.assertTrue(all(e["state"] == "skipped" for e in entries[1:]))
        self.assertEqual(self.read("runs/101-1/metadata.json")["collection_error"], "interrupted")

    def test_cancellation_does_not_target_a_later_human_rerun(self):
        coordinator = self.coordinator()
        coordinator.setup()
        entry = coordinator.suite["trials"][0]
        entry.update(run_id=101, run_attempt=1)
        coordinator.active = entry
        self.process.runs[101] = {"run_attempt": 2, "status": "in_progress"}
        coordinator.cancel()
        self.assertEqual(self.process.cancelled, [])

    def test_discovery_failure_persists_unresolved_dispatch_for_cleanup(self):
        self.process.list_hook = lambda runs: []
        self.assertEqual(self.coordinator(discovery_seconds=2).run(), 130)
        entry = self.read("suite.json")["trials"][0]
        self.assertTrue(entry["unresolved_dispatch"])
        self.assertEqual(entry["status"], "failed")
        self.assertNotIn("run_id", entry)
        self.assertEqual(len(self.process.dispatches), 1)

    def test_interrupt_during_discovery_recovers_and_cancels_dispatch(self):
        self.process.run_hook = lambda run, inputs: run.update(status="in_progress", conclusion=None)
        calls = 0
        def interrupted(runs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return runs
        self.process.list_hook = interrupted
        self.assertEqual(self.coordinator().run(), 130)
        self.assertEqual(self.process.cancelled, [101])
        self.assertEqual(len(self.process.dispatches), 1)
        self.assertFalse(self.read("suite.json")["trials"][0]["unresolved_dispatch"])

    def test_partial_artifacts_collected_after_poll_failure(self):
        self.process.run_hook = lambda run, inputs: run.update(status="in_progress", conclusion=None)
        self.process.fail_endpoint = "/attempts/1"
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        self.assertTrue((self.output / "runs/101-1/bench/result.json").exists())
        self.assertIn("collection_error", self.read("runs/101-1/metadata.json"))
        self.assertEqual(self.read("seeds.json")["seeds"], [])

    def test_timeout_during_collection_stops_new_children(self):
        original = self.process.respond
        def timed(endpoint, method, payload):
            if endpoint.endswith("/zip"):
                raise subprocess.TimeoutExpired("gh", 1)
            return original(endpoint, method, payload)
        self.process.respond = timed
        self.assertEqual(self.coordinator().run(), 130)
        self.assertEqual(len(self.process.dispatches), 1)
        self.assertIn("timed out", self.read("suite.json")["trials"][0]["error"])

    def test_seed_receipt_durable_before_later_download_interrupt(self):
        original = self.process.respond
        def interrupted(endpoint, method, payload):
            if "/actions/artifacts/" in endpoint:
                artifact_id = int(endpoint.split("/artifacts/")[1].split("/")[0])
                run_id, provision = divmod(artifact_id, 10)
                if provision and self.process.inputs[run_id]["scenario"] == "seed":
                    raise KeyboardInterrupt()
            return original(endpoint, method, payload)
        self.process.respond = interrupted
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "archil").run(), 130)
        self.assertEqual(len(self.read("seeds.json")["seeds"]), 1)
        self.assertFalse(any(p["inputs"]["scenario"] == "warm" for p in self.process.dispatches))

    def test_discovered_rerun_is_never_cancelled(self):
        self.process.run_hook = lambda run, inputs: run.update(
            run_attempt=2, status="in_progress", conclusion=None)
        self.assertEqual(self.coordinator("--workloads", "vue", "--providers", "github").run(), 1)
        self.assertEqual(self.process.cancelled, [])
        self.assertIn("unexpected rerun", self.read("suite.json")["trials"][0]["error"])

    def test_subprocess_request_timeouts_and_errors_are_bounded(self):
        def timeout(command, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 2)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        gh = suite.Gh(self.clock, timeout)
        gh.deadline = 2
        with self.assertRaises(suite.SuiteTimeout):
            gh.api("repos/example/project/commits/main")
        self.clock.now = 2
        with self.assertRaises(suite.SuiteTimeout):
            gh.api("anything")
        gh = suite.Gh(self.clock, lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, b"not json", b""))
        with self.assertRaisesRegex(suite.SuiteError, "invalid gh JSON"):
            gh.api("anything")

    def test_archive_traversal_symlink_and_corruption(self):
        for name in ("../escape", "/absolute", "folder\\escape", "C:/escape"):
            with self.subTest(name=name), self.assertRaises(suite.SuiteError):
                suite.unpack_archive(archive({name: "bad"}), self.output)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as output:
            link = zipfile.ZipInfo("link")
            link.external_attr = 0o120777 << 16
            output.writestr(link, "../target")
        with self.assertRaises(suite.SuiteError):
            suite.unpack_archive(data.getvalue(), self.output)
        with self.assertRaises(suite.SuiteError):
            suite.unpack_archive(b"not zip", self.output)

    def test_invalid_cli_and_unique_default_series(self):
        for extra in (("--trials", "0"), ("--trials", "11"), ("--series", "../bad"),
                      ("--providers", "github,github"), ("--workloads", "unknown"),
                      ("--output", "relative")):
            with self.subTest(extra=extra), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.args(*extra)
        base = ["--repository", "example/project", "--ref", "main",
                "--output", str(self.output)]
        with patch.dict("os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
            first, second = suite.parse_args(base), suite.parse_args(base)
        self.assertTrue(first.series.startswith("s-123-2-"))
        self.assertNotEqual(first.series, second.series)
        self.assertEqual(first.trials, 5)

    def test_workflow_path_is_trusted_and_existing_output_not_overwritten(self):
        self.process.workflow_path = ".github/workflows/untrusted.yml"
        self.assertEqual(self.coordinator().run(), 1)
        self.assertEqual(self.process.dispatches, [])
        before = (self.output / "suite.json").read_bytes()
        with self.assertRaises(suite.SuiteError):
            self.coordinator().run()
        self.assertEqual((self.output / "suite.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
