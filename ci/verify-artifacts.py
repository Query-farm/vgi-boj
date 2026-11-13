"""Install wheel and locked source archive in isolated directories and exercise SQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path) -> None:
    """Run a verification step and propagate its failure."""
    subprocess.run(command, cwd=cwd, check=True)


def main() -> None:
    """Verify both distributable formats outside the development checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.13")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "vgi-worker.json").read_text())["catalog"]
    wheels = list((ROOT / "dist").glob("*.whl"))
    archives = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(archives) != 1:
        raise SystemExit("Build exactly one wheel and one source archive before verification")
    with tempfile.TemporaryDirectory(prefix=f"vgi-{catalog}-artifacts-") as directory:
        temporary = Path(directory)
        wheel_env = temporary / "wheel-env"
        run(["uv", "venv", "--python", args.python, str(wheel_env)], temporary)
        run(["uv", "pip", "install", "--python", str(wheel_env / "bin/python"), str(wheels[0])], temporary)
        with tarfile.open(archives[0]) as archive:
            archive.extractall(temporary / "source", filter="data")
        source = next((temporary / "source").iterdir())
        run(["uv", "sync", "--locked", "--no-dev", "--no-editable", "--python", args.python], source)
        for label, environment in (("wheel", wheel_env), ("sdist", source / ".venv")):
            worker = environment / "bin" / f"vgi-{catalog}-worker"
            for suffix, options in (("", []), ("-http-auth", ["--http-auth"])):
                run(
                    [
                        "uv",
                        "run",
                        "--no-project",
                        "--with",
                        "duckdb==2.0.0",
                        "python",
                        str(ROOT / "test/run_duckdb.py"),
                        "--worker",
                        str(worker),
                        *options,
                        "--output",
                        str(ROOT / "build" / f"{label}{suffix}-verification.json"),
                    ],
                    temporary,
                )
    checksums = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in [*wheels, *archives]}
    (ROOT / "build/artifact-sha256.json").write_text(json.dumps(checksums, indent=2) + "\n")


if __name__ == "__main__":
    main()
