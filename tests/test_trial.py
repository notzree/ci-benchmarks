import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trial


class TrialTests(unittest.TestCase):
    def environment(self, **overrides):
        env = {
            "GITHUB_REPOSITORY": "example/benchmarks", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
            "CI_BENCH_PROVIDER": "github", "CI_BENCH_WORKLOAD": "vue",
            "CI_BENCH_SCENARIO": "cold", "CI_BENCH_SERIES": "test-series",
            "CI_BENCH_TRIAL": "1", "CI_BENCH_SEED_ID": "",
            "CI_BENCH_TEMPLATE_ID": "", "CI_BENCH_CACHE_KEY": "",
        }
        return {**env, **overrides}

    def test_cold_cannot_restore_seed(self):
        self.assertEqual(trial.inputs(self.environment())["seed_id"], "")
        for name in ("CI_BENCH_SEED_ID", "CI_BENCH_TEMPLATE_ID", "CI_BENCH_CACHE_KEY"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                trial.inputs(self.environment(**{name: "unexpected"}))

    def test_seed_key_is_unique_to_attempt(self):
        values = trial.inputs(self.environment(CI_BENCH_SCENARIO="seed"))
        self.assertEqual(values["seed_id"], "123-2-vue-github")
        self.assertEqual(values["cache_key"], "ci-bench-v1-Linux-X64-vue-123-2-vue-github-123-2")
        with self.assertRaises(ValueError):
            trial.inputs(self.environment(CI_BENCH_SCENARIO="seed", CI_BENCH_CACHE_KEY="old-key"))

    def test_archive_warm_requires_exact_identity_key(self):
        env = self.environment(
            CI_BENCH_SCENARIO="warm", CI_BENCH_SEED_ID="seed",
            CI_BENCH_CACHE_KEY="ci-bench-v1-Linux-X64-vue-seed-88-1",
        )
        self.assertEqual(trial.inputs(env)["seed_id"], "seed")
        for key in ("", "ci-bench-v1-Linux-X64-hugo-seed-88-1", "prefix\ninjected=true"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                trial.inputs({**env, "CI_BENCH_CACHE_KEY": key})

    def test_native_warm_requires_uuid(self):
        env = self.environment(
            CI_BENCH_PROVIDER="archil", CI_BENCH_SCENARIO="warm",
            CI_BENCH_SEED_ID="seed",
            CI_BENCH_TEMPLATE_ID="01234567-89ab-cdef-0123-456789abcdef",
        )
        self.assertEqual(trial.inputs(env)["provider"], "archil")
        with self.assertRaises(ValueError):
            trial.inputs({**env, "CI_BENCH_TEMPLATE_ID": "../other"})

    def test_input_injection_and_invalid_numbers(self):
        for field, value in [
            ("CI_BENCH_SERIES", "series\nother=true"),
            ("CI_BENCH_TRIAL", "01"), ("CI_BENCH_TRIAL", "0"),
            ("CI_BENCH_PROVIDER", "depot"), ("CI_BENCH_WORKLOAD", "private"),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                trial.inputs(self.environment(**{field: value}))

    def test_successful_receipt_requires_published_archive(self):
        with tempfile.TemporaryDirectory() as root:
            env = self.environment(
                CI_BENCH_SCENARIO="seed", CI_BENCH_SEED_ID="seed",
                CI_BENCH_CACHE_KEY="ci-bench-v1-Linux-X64-vue-seed-123-2",
                BENCH_OUTPUT=root,
            )
            result = {
                "schema_version": 1, "provider": "github", "workload": "vue",
                "scenario": "seed", "series": "test-series", "seed_id": "seed",
                "commit": "a" * 40, "run_id": 123, "run_attempt": 2,
                "outcome": "success",
            }
            path = Path(root) / "result.json"
            path.write_text(json.dumps(result))
            with self.assertRaises(ValueError):
                trial.write_receipt(env, run=lambda *args, **kwargs: SimpleNamespace(stdout='{"actions_caches": []}'))
            self.assertFalse((Path(root) / "seed.json").exists())
            cache = {"actions_caches": [{"key": env["CI_BENCH_CACHE_KEY"]}]}
            receipt = trial.write_receipt(env, run=lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(cache)))
            self.assertIsInstance(receipt["run_id"], int)
            self.assertEqual(json.loads((Path(root) / "seed.json").read_text()), receipt)
            path.write_text(json.dumps({**result, "outcome": "failure"}))
            with self.assertRaises(ValueError):
                trial.write_receipt(env)

    def test_native_receipt_has_no_archive_or_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            env = self.environment(
                CI_BENCH_PROVIDER="archil", CI_BENCH_SCENARIO="seed",
                CI_BENCH_SEED_ID="seed",
                CI_BENCH_TEMPLATE_ID="01234567-89ab-cdef-0123-456789abcdef",
                BENCH_OUTPUT=root, ARCHIL_API_KEY="dummy", GH_RUNNER_ADMIN_TOKEN="dummy",
            )
            result = {
                "schema_version": 1, "provider": "archil", "workload": "vue",
                "scenario": "seed", "series": "test-series", "seed_id": "seed",
                "commit": "a" * 40, "run_id": "123", "run_attempt": "2",
                "outcome": "success",
            }
            (Path(root) / "result.json").write_text(json.dumps(result))
            receipt = trial.write_receipt(env)
            self.assertEqual(receipt["cache_key"], "")
            self.assertNotIn("dummy", json.dumps(receipt))
            self.assertEqual(receipt["region"], "aws-us-east-1")


if __name__ == "__main__":
    unittest.main()
