"""Prepare and deploy this team's five runtimes using the supplied CDK project.

Nothing runs at import time. --prepare-only packages source without AWS access.
Existing lib directories are refused; only copies created here are cleaned up.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
AGENTS = ("ai-gk", "ai-def", "ai-mid", "ai-fwd1", "ai-fwd2")


def executable(name):
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable missing: {name}")
    return path


def run(name, *args, cwd=ROOT, capture=False):
    return subprocess.run([executable(name), *args], cwd=cwd, check=True,
                          text=True, encoding="utf-8", capture_output=capture)


def prepare():
    destinations = [ROOT / agent / "lib" for agent in AGENTS]
    for destination in destinations:
        if destination.exists():
            raise RuntimeError(f"Refusing to overwrite existing source: {destination}")
    created = []
    try:
        for destination in destinations:
            created.append(destination)
            shutil.copytree(ROOT.parent / "lib", destination,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test_*.py"))
    except BaseException:
        cleanup(created)
        raise
    return created


def cleanup(paths):
    allowed = {(ROOT / agent / "lib").resolve() for agent in AGENTS}
    for path in paths:
        resolved = path.resolve()
        if resolved not in allowed or path.is_symlink():
            raise RuntimeError(f"Refusing unexpected cleanup path: {resolved}")
        if path.exists():
            shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="Package shared source locally; retain copies for inspection")
    args = parser.parse_args()
    if args.prepare_only:
        for path in prepare():
            print(f"Prepared {path.relative_to(ROOT)}")
        return
    for name in ("agentcore", "aws", "node", "npm", "cdk", "uv"):
        executable(name)
    account = run("aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text", capture=True).stdout.strip()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        result = subprocess.run([executable("aws"), "configure", "get", "region"], capture_output=True, text=True)
        region = result.stdout.strip() or "us-east-1"
    (ROOT / "agentcore" / "aws-targets.json").write_text(
        json.dumps([{"name": "default", "account": account, "region": region}], indent=2) + "\n", encoding="utf-8")
    cdk = ROOT / "agentcore" / "cdk"
    if not (cdk / "node_modules").exists():
        run("npm", "ci", cwd=cdk)
    run("npm", "run", "build", cwd=cdk)
    run("cdk", "bootstrap", f"aws://{account}/{region}")
    copies = prepare()
    try:
        run("agentcore", "deploy", "--yes")
    finally:
        cleanup(copies)


if __name__ == "__main__":
    main()
