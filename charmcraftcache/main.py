import typer

app = typer.Typer()


@app.command()
def pack(cache: bool = True):
    print("pack")


@app.command()
def clean():
    print("clean")
