#!/usr/bin/env python3
"""Extract options, system variables, defaults and status variables from a
MySQL / Percona Server docker image and write data/<vendor>-<version>.json.

    extract.py oracle 8.4.11
    extract.py percona 8.4.11-11
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "mysql"

PLATFORM = []
HOSTNAME = "vdiffhost"

# Defaults such as general_log_file embed the container hostname. Replace it
# with a placeholder so defaults compare equal across versions.
_HOST_RE = re.compile(r"(?:(?<=/)|^)(?:[0-9a-f]{12}|" + HOSTNAME + r")(?=[.\-])")


# Defaults computed from the build itself (e.g. number of error messages), so
# they change on nearly every release without being a deliberate change.
DERIVED = {"performance_schema_error_size", "performance_schema_max_statement_classes"}


def default_source(name, from_help):
    return "derived" if name in DERIVED else ("help" if from_help else "runtime")


def scrub(value):
    return _HOST_RE.sub("<hostname>", value) if isinstance(value, str) else value


IMAGES = {
    "oracle": "mysql:{v}",
    "percona": "percona/percona-server:{v}",
}


def sh(*args, check=True, input=None):
    r = subprocess.run(args, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stderr}")
    return r.stdout


def norm(name):
    return name.strip().replace("-", "_")


def parse_help(text):
    """Return {name: {default, desc, arg}} from `mysqld --help --verbose`."""
    head, _, tail = text.partition("Variables (--variable-name=value)")
    opts = {}

    # Option blocks: "  --name=# " (optionally "  -x, --name[=type]") then
    # 22-space indented description lines.
    cur = None
    opt_re = re.compile(r"^  (?:-\w, )?--(?:\[skip-\])?([\w-]+)(\[?=[^\s\]]*\]?)?(?:\s+(\S.*))?\s*$")
    for line in head.splitlines():
        m = opt_re.match(line)
        if m:
            cur = norm(m.group(1))
            opts[cur] = {"arg": m.group(2) or "", "desc": []}
            if m.group(3):
                opts[cur]["desc"].append(m.group(3).strip())
        elif cur and line.startswith(" " * 10) and line.strip():
            opts[cur]["desc"].append(line.strip())
        else:
            cur = None
    for o in opts.values():
        o["desc"] = " ".join(o["desc"])

    # Values table
    values = {}
    started = False
    for line in tail.splitlines():
        if line.startswith("----"):
            started = True
            continue
        if not started or not line.strip():
            continue
        parts = re.split(r"\s{2,}|\s(?=\S+$)", line.rstrip(), maxsplit=1)
        m = re.match(r"^(\S+)\s+(.*)$", line.rstrip())
        if not m:
            continue
        name = norm(m.group(1))
        if re.fullmatch(r"[a-z0-9_]+", name):  # skips footer text such as "To see what values..."
            values[name] = m.group(2).strip()
    return opts, values


def live_queries(image):
    """Start a throwaway server and return sysvars / status names."""
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    sh(
        "docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name,
        "-e", "MYSQL_ALLOW_EMPTY_PASSWORD=1", image,
        "--performance-schema=ON",
    )
    try:
        for _ in range(300):
            time.sleep(2)
            r = subprocess.run(
                ["docker", "exec", name, "mysqladmin", "-uroot", "ping", "--silent"],
                capture_output=True,
            )
            # entrypoint runs a temporary server first; wait for the final one
            logs = sh("docker", "logs", name, check=False) + subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True
            ).stderr
            if r.returncode == 0 and "ready for connections" in logs and "MySQL init process done" in logs:
                break
        else:
            raise RuntimeError("server did not start")
        time.sleep(3)

        def q(sql):
            out = sh("docker", "exec", name, "mysql", "-uroot", "-N", "-B", "-e", sql)
            return [l.split("\t") for l in out.splitlines() if l]

        glob = {n: v for n, v in q("SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables")}
        sess = {r[0] for r in q("SELECT VARIABLE_NAME FROM performance_schema.session_variables")}
        info = {r[0]: r[1:] for r in q(
            "SELECT VARIABLE_NAME, VARIABLE_SOURCE, MIN_VALUE, MAX_VALUE FROM performance_schema.variables_info")}
        status = [r[0] for r in q("SELECT VARIABLE_NAME FROM performance_schema.global_status")]
        version = q("SELECT VERSION()")[0][0]

        # Reading a deprecated system variable raises warning 1287, and the
        # text usually names the replacement.
        script = "".join(
            f"SELECT 'VAR:{n}';\nSELECT @@global.`{n}`;\nSHOW WARNINGS;\n" for n in sorted(glob)
        )
        out = sh("docker", "exec", "-i", name, "mysql", "-uroot", "-N", "-B", "--force",
                 check=False, input=script)
        deprecated, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^VAR:(\S+)$", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"^Warning\t(?:1287|1681)\t(.*)$", line)
            if m and cur and "deprecated" in m.group(1):
                deprecated[cur] = m.group(1)
        return glob, sess, info, status, version, deprecated
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main():
    vendor, version = sys.argv[1], sys.argv[2]
    image = IMAGES[vendor].format(v=version)
    # only remove images this run pulled, never ones the user already had
    preexisting = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    try:
        extract(vendor, version, image)
    finally:
        if not preexisting:
            subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def extract(vendor, version, image):
    global PLATFORM
    if subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode != 0:
        # amd64-only image: fall back to emulation
        sh("docker", "pull", "-q", "--platform", "linux/amd64", image)
        PLATFORM = ["--platform", "linux/amd64"]
    print(f"[{vendor} {version}] --help --verbose", flush=True)
    help_text = sh("docker", "run", "--rm", *PLATFORM, "--hostname", HOSTNAME, "--entrypoint", "mysqld", image,
                   "--no-defaults", "--help", "--verbose", check=False)
    opts, values = parse_help(help_text)
    print(f"[{vendor} {version}] live server", flush=True)
    glob, sess, info, status, full_version, deprecated = live_queries(image)

    variables = {}
    for n in sorted(set(glob) | set(values)):
        if n not in glob and n not in opts:
            continue  # stray words from the --help footer, not a variable or option
        o = opts.get(n, {})
        entry = {
            "default": scrub(values.get(n, glob.get(n))),
            "default_source": default_source(n, n in values),
            "scope": ("both" if n in sess else "global") if n in glob else None,
            "cmdline_option": n in opts,
            "desc": o.get("desc", ""),
        }
        if n not in glob:
            entry["option_only"] = True  # startup option / plugin option, not a system variable
        if n in deprecated:
            msg = deprecated[n]
            entry["deprecated"] = msg
            r = re.search(r"(?:use|using)\s+(.+?)\s+instead", msg, re.I)
            if r:
                entry["replacement"] = r.group(1).replace("@@", "")
        elif re.search(r"\bdeprecated\b", entry["desc"], re.I):
            entry["deprecated"] = "Marked deprecated in the server option help text."
        if n in info:
            src, mn, mx = info[n]
            entry["min"] = None if mn in ("0", "") and mx in ("0", "") else mn
            entry["max"] = None if mn in ("0", "") and mx in ("0", "") else mx
        variables[n] = entry

    out = {
        "vendor": vendor,
        "version": version,
        "server_version": full_version,
        "series": ".".join(version.split(".")[:2]),
        "image": image,
        "variables": variables,
        "status": sorted(status),
    }
    DATA.mkdir(exist_ok=True)
    path = DATA / f"{vendor}-{version}.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"wrote {path} ({len(variables)} variables, {len(status)} status)")


if __name__ == "__main__":
    main()
