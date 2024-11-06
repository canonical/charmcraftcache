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

However, charmcraft builds are still slow the first time the wheel is built. This happens on CI runners, when you use a new machine/VM, or when you contribute to a new charm.

`charmcraftcache` solves the slow first build.

[charmcraftcache-hub](https://github.com/canonical/charmcraftcache-hub) maintains a [list of charms][list of charms]. For each charm, `charmcraft pack` is used to build Python dependencies from source and the pip wheel cache is uploaded to a GitHub release.

`ccc pack` downloads these pre-built wheels to charmcraft's pip cache (and then runs `charmcraft pack`).

Note: Within the GitHub release, each charm has an isolated cache. If the same charm (same GitHub repository and relative path to charmcraft.yaml) is added to the list of charms more than once (with different git refs), the wheels are combined into a single cache. If there are duplicate wheels, the wheel is selected from the ref that is earlier in the [list][list of charms].

### Isn't this just a worse version of PyPI?
Pretty much. The only difference is charmcraftcache-hub wheels are built from source on our runners, instead of built by the package maintainer.

[^1]: Unless `--destructive-mode` is enabled

[list of charms]: https://github.com/canonical/charmcraftcache-hub/blob/main/charms.json
