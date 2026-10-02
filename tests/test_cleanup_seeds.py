import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup_seeds


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.env = {
            "GITHUB_REPOSITORY": "example/benchmarks", "GITHUB_REPOSITORY_ID": "10",
            "GH_TOKEN": "dummy-actions", "ARCHIL_API_KEY": "dummy-archil",
            "GH_RUNNER_ADMIN_TOKEN": "dummy-admin",
        }
        self.seed = {
            "schema_version": 1, "repository": "example/benchmarks",
            "series": "series", "seed_id": "seed", "provider": "github",
            "workload": "vue", "run_id": 123, "run_attempt": 1,
            "commit": "a" * 40,
            "cache_key": "ci-bench-v1-Linux-X64-vue-seed-123-1",
            "template_id": "",
        }
        self.creator = {
            "id": 123, "run_attempt": 1, "status": "completed", "conclusion": "success",
            "head_sha": "a" * 40, "path": ".github/workflows/benchmark.yml",
        }
        self.manifest()

    def manifest(self, **overrides):
        suite = {
            "schema_version": 1, "repository": "example/benchmarks",
            "series": "series", "trials": [{"run_id": 123, "run_attempt": 1}],
        }
        (self.root / "suite.json").write_text(json.dumps({**suite, **overrides}))
        (self.root / "seeds.json").write_text(json.dumps({
            "schema_version": 1, "repository": "example/benchmarks",
            "series": "series", "seeds": [self.seed],
        }))

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "node":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(stdout=json.dumps(self.creator))

    def test_exact_archive_key_deleted(self):
        results = cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
        delete = [args for args, _ in self.calls if "DELETE" in args]
        self.assertEqual(len(delete), 1)
        self.assertIn("key=ci-bench-v1-Linux-X64-vue-seed-123-1", delete[0])
        self.assertEqual(results[0]["outcome"], "deleted")

    def test_foreign_or_unowned_receipt_fails_before_api(self):
        for changes in [
            {"repository": "other/repo"}, {"series": "other"},
            {"cache_key": "unrelated-cache"}, {"run_id": "../other"},
        ]:
            with self.subTest(changes=changes):
                original = self.seed
                self.seed = {**original, **changes}
                self.manifest()
                with self.assertRaises(ValueError):
                    cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
                self.assertEqual(self.calls, [])
                self.seed = original

    def test_unknown_or_active_child_prevents_eviction(self):
        self.manifest(trials=[{"unresolved_dispatch": True}])
        with self.assertRaises(ValueError):
            cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
        self.assertEqual(self.calls, [])
        self.manifest()
        self.creator = {**self.creator, "status": "in_progress"}
        with self.assertRaises(ValueError):
            cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
        self.assertFalse(any("DELETE" in args for args, _ in self.calls))

    def test_later_rerun_and_wrong_api_identity_refuse_delete(self):
        for changes in ({"run_attempt": 2}, {"id": 999}):
            with self.subTest(changes=changes):
                original = self.creator
                self.creator = {**original, **changes}
                with self.assertRaises(ValueError):
                    cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
                self.assertFalse(any("DELETE" in args for args, _ in self.calls))
                self.creator = original

    def test_wrong_creator_commit_refuses_delete(self):
        self.creator = {**self.creator, "head_sha": "b" * 40}
        with self.assertRaises(ValueError):
            cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run)
        self.assertFalse(any("DELETE" in args for args, _ in self.calls))
        self.assertEqual(json.loads((self.root / "cleanup.json").read_text())[0]["outcome"], "failure")

    def test_native_eviction_uses_original_attempt_namespace(self):
        self.seed = {
            **self.seed, "provider": "archil", "cache_key": "",
            "template_id": "01234567-89ab-cdef-0123-456789abcdef",
            "region": "aws-us-west-2",
        }
        self.manifest()
        action = self.root / "action"
        (action / "src").mkdir(parents=True)
        (action / "src" / "cli.mjs").write_text("// fake cleanup CLI\n")
        cleanup_seeds.cleanup(self.root, action, env=self.env, run=self.fake_run)
        node = [(args, kwargs) for args, kwargs in self.calls if args[0] == "node"]
        self.assertEqual(len(node), 1)
        cleanup_env = node[0][1]["env"]
        self.assertEqual(cleanup_env["GITHUB_RUN_ID"], "123")
        self.assertEqual(cleanup_env["GITHUB_RUN_ATTEMPT"], "1")
        self.assertEqual(cleanup_env["ARCHIL_CI_RETAIN_TEMPLATE"], "false")
        self.assertEqual(cleanup_env["ARCHIL_CI_TEMPLATE_ID"], "")
        self.assertEqual(cleanup_env["ARCHIL_CI_REGION"], "aws-us-west-2")
        self.assertEqual(node[0][0][-1], "cleanup")

    def test_missing_receipts_no_cloud_action(self):
        (self.root / "seeds.json").unlink()
        self.assertEqual(cleanup_seeds.cleanup(self.root, env=self.env, run=self.fake_run), [])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
