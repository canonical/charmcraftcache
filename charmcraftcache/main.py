import os
import pathlib
import shutil
import subprocess

import typer

app = typer.Typer()


@app.command()
def pack(cache: bool = True):
    if not shutil.which("charmcraft"):
        raise Exception("charmcraft not installed")
    # Check for charmcraft pack wrapper (tox `build` environment)
    try:
        tox_environments = subprocess.run(
            ["tox", "list", "--no-desc"],
            capture_output=True,
            check=True,
            encoding="utf-8",
        ).stdout.split("\n")
        if "build" in tox_environments:
            command = ["tox", "run", "-e", "build"]
        else:
            command = ["charmcraft", "pack"]
    except FileNotFoundError:
        command = ["charmcraft", "pack"]
    # Pack charm
    env = os.environ
    if cache:
        env["CRAFT_SHARED_CACHE"] = str(cache_directory)
        cache_directory.mkdir(parents=True, exist_ok=True)
        # TODO: download wheels
    # TODO: add status output
    subprocess.run(command, check=True, env=env)


@app.command()
def clean():
    # TODO: add status output
    try:
        shutil.rmtree(cache_directory)
    except FileNotFoundError:
        pass
    subprocess.run(["charmcraft", "clean"], check=True)


cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
