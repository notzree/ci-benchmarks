import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("workload", ROOT / "scripts/workload.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
probe_spec = importlib.util.spec_from_file_location("probe", ROOT / "scripts/workload_probe.py")
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "cache"
        self.output = self.base / "results"
        self.linux = patch.object(w, "linux_x64", return_value="x86_64")
        self.linux.start()
        self.system = patch.object(w, "system_info", return_value={
            "architecture": "x86_64", "logical_cpus": 4, "cpu_model": "fake",
            "memory_limit_bytes": 8000000000, "cpu_quota": 4,
        })
        self.system.start()
        self.env = patch.dict(os.environ, {
            "CI_BENCH_COMMIT": "consumer-sha", "CI_BENCH_RUN_ID": "123",
            "CI_BENCH_RUN_ATTEMPT": "2",
        })
        self.env.start()
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.system.stop()
        self.linux.stop()
        self.temp.cleanup()

    def args(self, scenario="cold", workload="vue"):
        return argparse.Namespace(provider="github", workload=workload, scenario=scenario,
                                  series="series-1", trial=1, seed_id="" if scenario == "cold" else "seed-1",
                                  cache_root=str(self.root), output=str(self.output))

    def fake_measure(self, command, cwd, env, log, timeout):
        self.calls.append((log.stem, command, cwd, env))
        log.write_text("fake command output\n")
        if log.stem == "setup":
            (self.root / "tools").mkdir(exist_ok=True)
            for name in w.tools_for(env["CI_BENCH_WORKLOAD"]):
                tool = w.manifest()["toolchains"][name]
                dest = self.root / "tools" / name
                (dest / "bin").mkdir(parents=True, exist_ok=True)
                (dest / tool["binary"]).write_text("fake binary")
                w.atomic_json(dest / ".receipt.json", tool)
        if log.stem == "checkout":
            (self.root / "state/source").mkdir(exist_ok=True)
            (self.root / "state/source/.git").mkdir(exist_ok=True)
            (self.root / "state/source/.git/HEAD").write_text(
                w.manifest()["workloads"][env["CI_BENCH_WORKLOAD"]]["upstream_sha"] + "\n")
        if log.stem == "install" and env["CI_BENCH_WORKLOAD"] == "vue":
            (self.root / "state/source/node_modules").mkdir(exist_ok=True)
        if log.stem == "build" and env["CI_BENCH_WORKLOAD"] == "hugo":
            (self.root / "state/bin/hugo").write_text("fake binary")
        return {"wall_seconds": 0.1, "user_seconds": 0.02, "system_seconds": 0.01,
                "max_rss_kib": 42, "exit_code": 0, "command": command}

    def invoke(self, args=None, failure=None):
        def measure(*items):
            result = self.fake_measure(*items)
            if items[3].stem == failure:
                result["exit_code"] = 17
            return result
        with patch.object(w, "measure", side_effect=measure):
            return w.run(args or self.args())

    def result(self):
        return json.loads((self.output / "result.json").read_text())

    def seed(self, workload="vue"):
        self.assertEqual(self.invoke(self.args("seed", workload)), 0)
        self.calls.clear()

    def test_cold_clears_state_not_tools_and_runs_real_command_contract(self):
        (self.root / "state").mkdir(parents=True)
        (self.root / "state/dirty").write_text("old")
        (self.root / "tools").mkdir()
        (self.root / "tools/preserved").write_text("tool")
        self.assertEqual(self.invoke(), 0)
        self.assertFalse((self.root / "state/dirty").exists())
        self.assertTrue((self.root / "tools/preserved").exists())
        result = self.result()
        self.assertEqual(result["cache"]["validation"], "empty")
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["commit"], os.environ.get("GITHUB_SHA", "consumer-sha"))
        phases = {name: command for name, command, _, _ in self.calls}
        self.assertEqual(phases["build"], ["pnpm", "build"])
        self.assertEqual(phases["install"], [
            "pnpm", "install", "--frozen-lockfile", "--store-dir", str(self.root / "state/pnpm-store")])
        self.assertEqual(phases["test"], ["pnpm", "test-unit", "--run", "--maxWorkers=4", "--minWorkers=1"])
        self.assertFalse((self.root / "seed-marker.json").exists())
        self.assertTrue((self.output / "result.csv").exists())

    def test_seed_warm_reuses_state_and_keeps_seed_files_immutable(self):
        self.seed()
        marker = (self.root / "seed-marker.json").read_bytes()
        result = (self.root / "seed-result.json").read_bytes()
        (self.root / "state/keep").write_text("dependency")
        self.assertEqual(self.invoke(self.args("warm")), 0)
        self.assertEqual(self.result()["cache"]["validation"], "hit")
        self.assertEqual(marker, (self.root / "seed-marker.json").read_bytes())
        self.assertEqual(result, (self.root / "seed-result.json").read_bytes())
        self.assertEqual((self.root / "state/keep").read_text(), "dependency")
        commands = {name: command for name, command, _, _ in self.calls}
        self.assertEqual(commands["build"], ["pnpm", "build"])
        self.assertIn("--warm", commands["checkout"])

    def test_seed_failure_never_writes_marker_and_outputs_failure_metrics(self):
        self.assertEqual(self.invoke(self.args("seed"), failure="probe"), 1)
        result = self.result()
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(result["phases"]["probe"]["exit_code"], 17)
        self.assertEqual(result["phases"]["total"]["exit_code"], 1)
        self.assertFalse((self.root / "seed-marker.json").exists())
        self.assertFalse((self.root / "seed-result.json").exists())
        self.assertTrue((self.output / "probe.log").exists())
        self.assertIn("probe", (self.output / "error.log").read_text())

    def test_warm_miss_fails_before_setup(self):
        self.assertEqual(self.invoke(self.args("warm")), 1)
        self.assertEqual(self.result()["cache"]["validation"], "miss")
        self.assertEqual(self.calls, [])
        self.assertEqual(set(self.result()["phases"]), {"total"})

    def test_every_identity_mismatch_and_corruption_rejected(self):
        self.seed()
        original = (self.root / "seed-marker.json").read_bytes()
        for key in ("workload", "seed_id", "upstream_sha", "fingerprint",
                    "toolchains", "architecture", "cache_root", "outcome"):
            with self.subTest(key=key):
                marker = json.loads(original)
                marker[key] = "wrong"
                w.atomic_json(self.root / "seed-marker.json", marker)
                self.assertEqual(self.invoke(self.args("warm")), 1)
                self.assertEqual(self.calls, [])
        for payload in (b"{", b"[]", b'{"schema_version":1,"schema_version":1}',
                        b"x" * (w.JSON_LIMIT + 1)):
            (self.root / "seed-marker.json").write_bytes(payload)
            self.assertEqual(self.invoke(self.args("warm")), 1)
        (self.root / "seed-marker.json").write_bytes(original)
        (self.root / "seed-result.json").write_text("{}")
        self.assertEqual(self.invoke(self.args("warm")), 1)
        self.assertEqual(self.calls, [])

    def test_wrong_seed_id_missing_state_and_changed_result_rejected(self):
        self.seed()
        with self.assertRaises(ValueError):
            w.validate_seed("vue", self.root, "not-the-seed")
        w.remove_tree(self.root / "state/source")
        with self.assertRaises(ValueError):
            w.validate_seed("vue", self.root, "seed-1")

    def test_verify_seed_copies_measurement_without_commands(self):
        self.seed()
        argv = ["workload.py", "verify-seed", "--workload", "vue", "--cache-root", str(self.root),
                "--seed-id", "seed-1", "--output", str(self.base / "copy")]
        with patch.object(sys, "argv", argv), patch.object(w, "measure", side_effect=AssertionError):
            self.assertEqual(w.main(), 0)
        self.assertEqual((self.base / "copy/result.json").read_bytes(),
                         (self.root / "seed-result.json").read_bytes())

    def test_verify_seed_miss_writes_failure_record(self):
        argv = ["workload.py", "verify-seed", "--workload", "vue", "--cache-root", str(self.root),
                "--seed-id", "seed-1", "--output", str(self.output)]
        with patch.object(sys, "argv", argv):
            self.assertEqual(w.main(), 1)
        self.assertEqual(self.result()["outcome"], "failure")
        self.assertEqual(self.result()["cache"]["validation"], "miss")
        self.assertTrue((self.output / "result.csv").exists())
        self.assertTrue((self.output / "error.log").exists())

    def test_same_cache_concurrent_run_fails_before_destructive_cleanup(self):
        self.seed()
        marker = (self.root / "seed-marker.json").read_bytes()
        with (self.root / ".workload.lock").open("a") as lock:
            w.fcntl.flock(lock, w.fcntl.LOCK_EX | w.fcntl.LOCK_NB)
            self.assertEqual(self.invoke(self.args("cold")), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(marker, (self.root / "seed-marker.json").read_bytes())

    def test_wrong_cached_checkout_sha_fails_warm(self):
        self.seed()
        (self.root / "state/source/.git/HEAD").write_text("wrong-sha\n")
        self.assertEqual(self.invoke(self.args("warm")), 1)
        self.assertEqual(self.calls, [])

    def test_hugo_has_real_compile_and_no_fake_test_phase(self):
        self.seed("hugo")
        self.assertNotIn("test", self.result()["phases"])
        build = self.result()["phases"]["build"]["command"]
        self.assertEqual(build, ["go", "build", "-mod=readonly", "-p", "4", "-o",
                                 str(self.root / "state/bin/hugo"), "."])

    def test_environment_isolated_and_bounded_tool_caches(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
                                    "NPM_CONFIG_USERCONFIG": "/secret", "GOFLAGS": "-bad"}):
            env = w.environment(self.root, "vue")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("NPM_CONFIG_USERCONFIG", env)
        self.assertEqual(env["PUPPETEER_SKIP_DOWNLOAD"], "true")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOMAXPROCS"], "4")
        self.assertEqual(env["GOFLAGS"], "-mod=readonly")
        self.assertTrue(env["GOCACHE"].startswith(str(self.root)))

    def test_safe_paths_and_symlinks(self):
        for path in ("/", str(Path.home()), str(w.REPO), str(w.REPO / "cache"),
                     str(w.REPO.parent), "relative", str(self.base / "../cache")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                w.cache_path(path)
        link = self.base / "link"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            w.cache_path(str(link / "cache"))
        self.root.mkdir()
        (self.root / "state").symlink_to(self.base, target_is_directory=True)
        self.assertEqual(self.invoke(), 1)
        self.assertIn("Symlink", self.result()["error"])
        self.assertTrue(self.base.exists())

    def test_seed_output_is_only_allowed_cache_output(self):
        allowed = self.root / "seed-output"
        self.assertEqual(w.output_path(str(allowed), self.root, seed=True), allowed)
        with self.assertRaises(ValueError):
            w.output_path(str(allowed), self.root)
        with self.assertRaises(ValueError):
            w.output_path(str(self.root / "state/output"), self.root, seed=True)

    def test_measure_real_subprocess_resources_and_failure(self):
        self.output.mkdir()
        metrics = w.measure([sys.executable, "-c", "sum(i*i for i in range(2000000))"],
                            self.base, os.environ.copy(), self.output / "cpu.log", 10)
        self.assertEqual(metrics["exit_code"], 0)
        self.assertGreater(metrics["wall_seconds"], 0)
        self.assertGreater(metrics["user_seconds"], 0)
        self.assertGreater(metrics["max_rss_kib"], 0)
        failed = w.measure([sys.executable, "-c", "raise SystemExit(9)"],
                           self.base, os.environ.copy(), self.output / "fail.log", 10)
        self.assertEqual(failed["exit_code"], 9)

    def test_resource_peaks_are_not_cumulative(self):
        self.output.mkdir()
        large = w.measure([sys.executable, "-c", "x=bytearray(64*1024*1024)"],
                          self.base, os.environ.copy(), self.output / "large.log", 10)
        small = w.measure([sys.executable, "-c", "pass"],
                          self.base, os.environ.copy(), self.output / "small.log", 10)
        self.assertGreater(large["max_rss_kib"], small["max_rss_kib"] + 30000)

    def test_missing_executable_and_cancellation_have_metrics(self):
        self.output.mkdir()
        missing = w.measure(["/nonexistent/workload-command"], self.base, os.environ.copy(),
                            self.output / "missing.log", 10)
        self.assertEqual(missing["exit_code"], 127)
        with patch.object(w.time, "sleep", side_effect=KeyboardInterrupt):
            cancelled = w.measure([sys.executable, "-c", "import time; time.sleep(30)"],
                                  self.base, os.environ.copy(), self.output / "cancelled.log", 10)
        self.assertEqual(cancelled["exit_code"], 130)
        self.assertTrue(cancelled["cancelled"])

    def test_cgroup_nested_memory_quota_and_cpuset(self):
        self.system.stop()
        proc = self.base / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "cpuinfo").write_text("model name : Fake CPU\n")
        (proc / "meminfo").write_text("MemTotal: 64000000 kB\n")
        (proc / "self/cgroup").write_text("0::/parent/child\n")
        cgroup = self.base / "cgroup"
        child = cgroup / "parent/child"
        child.mkdir(parents=True)
        (child / "memory.max").write_text("max\n")
        (child.parent / "memory.max").write_text("8000000000\n")
        (child / "cpu.max").write_text("400000 100000\n")
        (child.parent / "cpu.max").write_text("200000 100000\n")
        (child / "cpuset.cpus.effective").write_text("0-3\n")
        with patch.object(w.os, "sched_getaffinity", return_value={0, 1, 2, 3}, create=True):
            info = w.system_info(proc, cgroup)
        self.system.start()
        self.assertEqual(info["memory_limit_bytes"], 8000000000)
        self.assertEqual(info["cpu_quota"], 2)
        self.assertEqual(info["cpuset"], "0-3")
        self.assertEqual(info["cpu_model"], "Fake CPU")
        self.assertEqual(info["cgroup_version"], 2)

    def test_clear_readonly_go_cache_without_following_symlinks(self):
        tree = self.root / "state/go-mod/package"
        tree.mkdir(parents=True)
        (tree / "mod.go").write_text("package")
        (tree / "mod.go").chmod(0o444)
        (tree / "outside").symlink_to(self.base, target_is_directory=True)
        tree.chmod(0o555)
        w.remove_tree(self.root / "state")
        self.assertFalse((self.root / "state").exists())
        self.assertTrue(self.base.exists())

    def test_timeout_kills_subprocess_group(self):
        self.output.mkdir()
        metrics = w.measure([sys.executable, "-c",
                             "import subprocess,time; subprocess.Popen(['sleep','30']); time.sleep(30)"],
                            self.base, os.environ.copy(), self.output / "timeout.log", 0.1)
        self.assertEqual(metrics["exit_code"], 124)
        self.assertTrue(metrics["timed_out"])
        self.assertLess(metrics["wall_seconds"], 3)

    def test_failed_install_stops_later_phases(self):
        self.assertEqual(self.invoke(failure="install"), 1)
        self.assertNotIn("build", self.result()["phases"])
        self.assertNotIn("probe", self.result()["phases"])

    def test_bootstrap_local_archive_checksum_and_idempotence(self):
        archive = self.base / "tool.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            data = b"fake pinned binary"
            member = tarfile.TarInfo("node/bin/node")
            member.size = len(data)
            member.mode = 0o755
            tar.addfile(member, io.BytesIO(data))
        config = w.manifest()
        config["workloads"]["vue"]["tools"] = ["node"]
        config["toolchains"]["node"].update(
            prefix="node", sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        downloads = []
        def checked(command, **kwargs):
            downloads.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(archive.read_bytes())
        with patch.object(w, "manifest", return_value=config), \
                patch.object(w.shutil, "which", return_value="/usr/bin/fake"), \
                patch.object(w.Path, "is_file", autospec=True, side_effect=lambda p: (
                    True if str(p) == "/etc/ssl/certs/ca-certificates.crt" else p.exists())), \
                patch.object(w, "checked", side_effect=checked), \
                patch.object(w.subprocess, "check_output", return_value="v22.11.0\n"):
            w.bootstrap(self.root, "vue")
            w.bootstrap(self.root, "vue")
            self.assertEqual(len(downloads), 1)
            config["toolchains"]["node"]["sha256"] = "bad"
            with self.assertRaisesRegex(ValueError, "checksum"):
                w.bootstrap(self.root, "vue")

    def test_nonroot_bootstrap_never_attempts_sudo_or_apt(self):
        with patch.object(w.shutil, "which", return_value=None), \
                patch.object(w.os, "geteuid", return_value=1000), \
                patch.object(w, "checked", side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError, "no sudo"):
                w.bootstrap(self.root, "vue")

    def test_seed_publication_failure_propagates_and_removes_partial_marker(self):
        original = w.atomic_json
        def fail_marker(path, value):
            if path.name == "seed-marker.json":
                raise OSError("fake disk failure")
            original(path, value)
        with patch.object(w, "atomic_json", side_effect=fail_marker):
            self.assertEqual(self.invoke(self.args("seed")), 1)
        self.assertEqual(self.result()["outcome"], "failure")
        self.assertIn("publication", self.result()["error"])
        self.assertFalse((self.root / "seed-result.json").exists())
        self.assertFalse((self.root / "seed-marker.json").exists())

    def test_missing_tool_or_dependencies_is_warm_miss(self):
        self.seed()
        (self.root / "tools/node/bin/node").unlink()
        self.assertEqual(self.invoke(self.args("warm")), 1)
        self.assertEqual(self.calls, [])

    def test_vue_probe_invokes_cjs_production_assertions_and_propagates_failure(self):
        argv = ["probe", "vue", str(self.root / "state"), "3.5.13"]
        with patch.object(sys, "argv", argv), patch.object(probe.subprocess, "run") as run:
            probe.main()
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["node", "-e"])
            self.assertEqual(command[-1], "3.5.13")
            self.assertTrue(command[-2].endswith("vue.cjs.prod.js"))
            self.assertIn("vue.computed", command[2])
            self.assertIn("assert.equal(observed, 14)", command[2])
        with patch.object(sys, "argv", argv), patch.object(
                probe.subprocess, "run", side_effect=w.subprocess.CalledProcessError(1, ["node"])):
            with self.assertRaises(w.subprocess.CalledProcessError):
                probe.main()

    def test_hugo_readonly_download_detects_mutation(self):
        source = self.root / "state/source"
        source.mkdir(parents=True)
        (source / "go.mod").write_text("module fake")
        (source / "go.sum").write_text("sum")
        with patch.object(w, "checked", side_effect=lambda *a, **k: (source / "go.sum").write_text("changed")):
            with self.assertRaisesRegex(ValueError, "readonly"):
                w.hugo_download(self.root)

    def test_production_hugo_probe_validates_version_and_actual_html(self):
        state = self.root / "state"
        (state / "tmp").mkdir(parents=True)
        argv = ["probe", "hugo", str(state), "0.139.3"]
        def render(command, **kwargs):
            site = Path(command[command.index("--source") + 1])
            self.assertIn("{{ .Content }}", (site / "layouts/index.html").read_text())
            dest = Path(command[command.index("--destination") + 1])
            dest.mkdir()
            (dest / "index.html").write_text("<title>CI benchmark</title><strong>production</strong>")
        with patch.object(sys, "argv", argv), \
                patch.object(probe.subprocess, "check_output", return_value="hugo v0.139.3 linux/amd64\n"), \
                patch.object(probe.subprocess, "run", side_effect=render):
            probe.main()
        with patch.object(sys, "argv", argv), \
                patch.object(probe.subprocess, "check_output", return_value="hugo v9.9.9 linux/amd64\n"):
            with self.assertRaisesRegex(RuntimeError, "version"):
                probe.main()
        def bad_render(command, **kwargs):
            dest = Path(command[command.index("--destination") + 1])
            dest.mkdir()
            (dest / "index.html").write_text("not rendered")
        with patch.object(sys, "argv", argv), \
                patch.object(probe.subprocess, "check_output", return_value="hugo v0.139.3 linux/amd64\n"), \
                patch.object(probe.subprocess, "run", side_effect=bad_render):
            with self.assertRaisesRegex(RuntimeError, "HTML"):
                probe.main()

    def test_platform_and_cli_validation(self):
        self.linux.stop()
        with patch.object(w.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(ValueError, "Linux x64"):
                w.linux_x64()
        self.linux.start()
        for value in ("../x", "with spaces", "", "a" * 81):
            with self.assertRaises(argparse.ArgumentTypeError):
                w.slug(value)
        with self.assertRaises(argparse.ArgumentTypeError):
            w.positive("0")


if __name__ == "__main__":
    unittest.main()
