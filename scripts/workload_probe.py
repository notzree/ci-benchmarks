"""Correctness checks executed as timed subprocesses by workload.py."""

import argparse
import pathlib
import re
import subprocess
import tempfile


VUE_PROBE = """
const assert = require('node:assert/strict');
const vue = require(process.argv[1]);
assert.equal(vue.version, process.argv[2]);
const state = vue.reactive({ count: 1 });
const doubled = vue.computed(() => state.count * 2);
assert.equal(vue.isReactive(state), true);
assert.equal(doubled.value, 2);
let observed;
const runner = vue.effect(() => { observed = doubled.value });
state.count = 7;
assert.equal(doubled.value, 14);
assert.equal(observed, 14);
vue.stop(runner);
console.log('Vue production CJS reactivity verified');
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workload", choices=["vue", "hugo"])
    parser.add_argument("state", type=pathlib.Path)
    parser.add_argument("version")
    args = parser.parse_args()
    if args.workload == "vue":
        subprocess.run(
            ["node", "-e", VUE_PROBE,
             str(args.state / "source/packages/vue/dist/vue.cjs.prod.js"), args.version],
            check=True,
        )
    else:
        binary = args.state / "bin/hugo"
        version = subprocess.check_output([str(binary), "version"], text=True)
        if not re.search(r"\bhugo v" + re.escape(args.version) + r"(?:[-+\s]|$)", version):
            raise RuntimeError("Hugo binary version does not match pinned upstream")
        print(version, end="")
        with tempfile.TemporaryDirectory(dir=args.state / "tmp", prefix="site-") as tmp:
            site = pathlib.Path(tmp)
            (site / "content").mkdir()
            (site / "layouts").mkdir()
            (site / "hugo.toml").write_text('baseURL = "https://example.invalid/"\n')
            (site / "content/_index.md").write_text(
                '---\ntitle: "CI benchmark"\n---\nReal **production** render.\n'
            )
            (site / "layouts/index.html").write_text(
                "<!doctype html><html><title>{{ .Title }}</title>"
                "<body>{{ .Content }}</body></html>"
            )
            subprocess.run([str(binary), "--source", str(site), "--destination",
                            str(site / "public")], check=True)
            html = (site / "public/index.html").read_text()
            if "<title>CI benchmark</title>" not in html or "<strong>production</strong>" not in html:
                raise RuntimeError("Hugo generated HTML failed correctness check")
            print("Hugo production HTML verified")


if __name__ == "__main__":
    main()
