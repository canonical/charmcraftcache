# charmcraftcache
_Reinventing the wheel_

Fast first-time builds for [charmcraft](https://github.com/canonical/charmcraft)—on a local machine or CI

## Installation
Install `pipx`: https://pipx.pypa.io/stable/installation/
```
pipx install charmcraftcache
```

## Usage
```
ccc add
ccc pack
```

For best results, add `charm-strict-dependencies: true` to your charmcraft.yaml.

## How it works
### Why are charmcraft builds slow?
Instead of downloading wheels from PyPI (which pip does by default), charmcraft builds Python package wheels from source (i.e. with pip install [--no-binary](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-no-binary)).

### Caching mechanism
charmcraft builds each charm base in a separate LXC container[^1]. Within each container, pip has an [internal cache](https://pip.pypa.io/en/stable/topics/caching/) for wheels built from source & for HTTP responses.

charmcraft 2.5 moved the pip internal cache to the LXC host machine, so that one pip cache is used for all LXC containers. (This increases the chance of a cache hit—a faster build.)

However, charmcraft builds are still slow the first time the wheel is built. This happens on CI runners, when you update dependencies, use a new machine/VM, or contribute to a new charm.

`charmcraftcache` solves the slow first build.

[charmcraftcache-hub](https://github.com/canonical/charmcraftcache-hub) maintains a [list of charms](https://github.com/canonical/charmcraftcache-hub/blob/main/charms.json). For each charm, every Python dependency is built from source & uploaded to a GitHub release.

`ccc pack` downloads these pre-built wheels to charmcraft's pip cache (and then runs `charmcraft pack`).

### Isn't this just a worse version of PyPI?
Pretty much. The only difference is charmcraftcache-hub wheels are built from source on our runners, instead of built by the package maintainer.

[^1]: Unless `--destructive-mode` is enabled