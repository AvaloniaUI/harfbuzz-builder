#!/usr/bin/env python3
"""Resolves a HarfBuzz version and prepares a checkout of it in ./harfbuzz.

  fetch-harfbuzz.py --ref 15.1.0 --resolve-only     prints the resolved version as JSON (no checkout)
  fetch-harfbuzz.py --ref 15.1.0                    checks out ./harfbuzz

--ref is a tag, a branch, or a full commit SHA. --repository is a GitHub repository (owner/name), or a git URL or a
local path for local builds. The release tag is <version>-<sha10>, where <version> is the project version in the
meson.build of that commit (for example 15.1.0-e079f0563a).
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARFBUZZ = ROOT / "harfbuzz"


def run(args, cwd=None, capture=False):
    result = subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def is_github(repository):
    return re.fullmatch(r"[\w.-]+/[\w.-]+", repository) is not None and not pathlib.Path(repository).exists()


def remote_url(repository):
    if is_github(repository):
        return f"https://github.com/{repository}.git"
    if pathlib.Path(repository).exists():
        return pathlib.Path(repository).resolve().as_uri()
    return repository


def resolve(repository, ref):
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    out = run(["git", "ls-remote", remote_url(repository), f"refs/heads/{ref}", f"refs/tags/{ref}",
               f"refs/tags/{ref}^{{}}"], capture=True)
    refs = dict(reversed(line.split("\t")) for line in out.splitlines() if line)
    for key in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if key in refs:
            return refs[key]
    sys.exit(f"{ref} is not a branch, tag or full commit SHA of {repository}")


def version_from(text):
    match = re.search(r"project\s*\(\s*'harfbuzz'.*?version\s*:\s*'([0-9][0-9.]*)'", text, re.S)
    if not match:
        sys.exit("The project version was not found in meson.build")
    return match.group(1)


def fetch(repository, sha, dest):
    dest.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=dest)
    run(["git", "remote", "add", "origin", remote_url(repository)], cwd=dest)
    run(["git", "fetch", "-q", "--depth", "1", "origin", sha], cwd=dest)
    run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dest)


def remote_version(repository, sha):
    if is_github(repository):
        url = f"https://raw.githubusercontent.com/{repository}/{sha}/meson.build"
        with urllib.request.urlopen(url, timeout=60) as response:
            return version_from(response.read().decode())
    with tempfile.TemporaryDirectory() as temp:
        fetch(repository, sha, pathlib.Path(temp) / "harfbuzz")
        return version_from((pathlib.Path(temp) / "harfbuzz" / "meson.build").read_text())


def checkout(repository, sha):
    if (HARFBUZZ / ".git").exists():
        head = run(["git", "rev-parse", "HEAD"], cwd=HARFBUZZ, capture=True).strip()
        if head == sha:
            return
        shutil.rmtree(HARFBUZZ)
    fetch(repository, sha, HARFBUZZ)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", default="harfbuzz/harfbuzz",
                        help="GitHub repository of HarfBuzz (default harfbuzz/harfbuzz), a git URL or a local path")
    parser.add_argument("--ref", required=True, help="tag, branch or full commit SHA")
    parser.add_argument("--resolve-only", action="store_true", help="print the resolved version without a checkout")
    parser.add_argument("--github-output", action="store_true", help="also write the result to $GITHUB_OUTPUT")
    args = parser.parse_args()

    sha = resolve(args.repository, args.ref)
    if args.resolve_only:
        version = remote_version(args.repository, sha)
    else:
        checkout(args.repository, sha)
        version = version_from((HARFBUZZ / "meson.build").read_text())

    info = {"repository": args.repository, "ref": args.ref, "sha": sha, "version": version,
            "tag": f"{version}-{sha[:10]}"}
    print(json.dumps(info, indent=2))
    if args.github_output and "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            for key, value in info.items():
                out.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
