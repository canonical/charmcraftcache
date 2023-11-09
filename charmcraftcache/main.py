import json
import os
import pathlib
import shutil
import subprocess
import sys

import requests
import typer

app = typer.Typer()


@app.command()
def pack(cache: bool = True):
    cache_directory.mkdir(parents=True, exist_ok=True)
    report_file = cache_directory / "report.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "-r",
            "requirements.txt",
            "--ignore-installed",
            "--report",
            str(report_file),
        ],
        check=True,
    )
    with open(report_file, "r") as file:
        report = json.load(file)
    dependencies = report["install"]
    # Pack charm
    env = os.environ
    if cache:
        env["CRAFT_SHARED_CACHE"] = str(cache_directory)
        charmcraft_cache_subdirectory = (
            cache_directory / "charmcraft-buildd-base-v2.0/BuilddBaseAlias.JAMMY"
        )
        charmcraft_cache_subdirectory.mkdir(parents=True, exist_ok=True)
        response = requests.get(
            "https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        print("foo")
        for asset in response.json()["assets"]:
            for dependency in dependencies:
                if asset["name"].startswith(
                    f'{dependency["metadata"]["name"]}-{dependency["metadata"]["version"]}-'
                ):
                    # Download wheel
                    response = requests.get(
                        f'https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/assets/{asset["id"]}',
                        headers={
                            "Accept": "application/octet-stream",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        stream=True,
                    )
                    name, parent = (
                        asset["name"]
                        .removesuffix(".charmcraftcachehub")
                        .split(".charmcraftcachehub.")
                    )
                    parent = parent.replace("_", "/")
                    file_path = charmcraft_cache_subdirectory / parent / name
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "wb") as file:
                        for chunk in response.iter_content():
                            file.write(chunk)
                    print("downloaded")
                    break
    print("packing")
    # TODO: add status output
    try:
        subprocess.run(["charmcraft", "pack", "-v"], check=True, env=env)
    except FileNotFoundError:
        raise Exception("charmcraft not installed")
    except subprocess.CalledProcessError as e:
        raise Exception(e.stderr)


@app.command()
def clean():
    # TODO: add status output
    try:
        shutil.rmtree(cache_directory)
    except FileNotFoundError:
        pass
    subprocess.run(["charmcraft", "clean"], check=True)


cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
