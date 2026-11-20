# Gentoo packaging

`dev-util/vigil/vigil-9999.ebuild` is a live ebuild: it builds vigil from the `main` branch of this
repository with `git-r3`, so `emerge` installs and upgrades it. There are no tagged releases yet, so
there is no versioned ebuild and no `KEYWORDS`.

The author's overlay carries the same file, copied unchanged from here. This directory is the
source of truth: change the ebuild here, then copy it into the overlay.

## Install

As root, with `app-eselect/eselect-repository` installed, add the overlay and sync it. The overlay is
named `nick`:

    eselect repository add nick git https://github.com/SDNick484/gentoo-nick.git
    emaint sync -r nick

A live ebuild has no `KEYWORDS`, so `~amd64` is not enough; accept it with `**`:

    echo 'dev-util/vigil **' >> /etc/portage/package.accept_keywords/vigil

Then install it. The ebuild needs Python 3.14 or 3.15 in `PYTHON_TARGETS`:

    emerge --ask dev-util/vigil

Portage does not upgrade a live ebuild on its own. To pick up the newest `main`, rebuild it:

    emerge --ask --oneshot dev-util/vigil

## After installing

The ebuild installs the program, and the service files from `contrib/` under
`/usr/share/vigil/contrib`. It does not install an XDG autostart entry or an enabled service. Each
user runs this as their own user, not as root:

    vigil setup

then `vigil --version` and `vigil scan` to check that it works. See the main README for the rest.

## Checking the ebuild

CI runs `bash -n` and `xmllint` on the files here; it does not run `pkgcheck`, which needs a
Gentoo container with the Portage tree. To run it yourself on Gentoo, in a scratch copy that has the
minimal repository files `pkgcheck` needs (they are not kept in this directory, so the copy into the
overlay stays unchanged):

    d=$(mktemp -d)
    mkdir -p "$d/profiles" "$d/metadata"
    echo vigil-check > "$d/profiles/repo_name"
    echo 'masters = gentoo' > "$d/metadata/layout.conf"
    cp -r packaging/gentoo/dev-util "$d/"
    touch "$d/dev-util/vigil/Manifest"
    (cd "$d" && pkgcheck scan)

`tests/test_packaging.py` keeps the ebuild's `RDEPEND` in step with `pyproject.toml`: a new
dependency there fails CI until the ebuild lists it too.
