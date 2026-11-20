# Copyright 2026 Nick Andrade
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DISTUTILS_USE_PEP517=hatchling
PYTHON_COMPAT=( python3_{14..15} )
inherit distutils-r1 git-r3

DESCRIPTION="Local service, web UI and CLI to index and resume Claude Code sessions"
HOMEPAGE="https://github.com/SDNick484/vigil"
EGIT_REPO_URI="https://github.com/SDNick484/vigil.git"

LICENSE="MIT"
SLOT="0"
# Live ebuild: no KEYWORDS. Accept it with ** in package.accept_keywords.

# Keep in step with "dependencies" in pyproject.toml; tests/test_packaging.py
# fails when the two lists differ.
RDEPEND="
	dev-python/fastapi[${PYTHON_USEDEP}]
	dev-python/uvicorn[${PYTHON_USEDEP}]
	dev-python/jinja2[${PYTHON_USEDEP}]
	dev-python/watchfiles[${PYTHON_USEDEP}]
	dev-python/python-multipart[${PYTHON_USEDEP}]
	dev-python/httpx[${PYTHON_USEDEP}]
	dev-python/tomlkit[${PYTHON_USEDEP}]
"
# The tests need nothing beyond pytest (added by distutils_enable_tests) and
# httpx, which is already a run-time dependency, so there is no test? BDEPEND.
# They use no pytest plugins.
EPYTEST_PLUGINS=()
distutils_enable_tests pytest

python_install_all() {
	distutils-r1_python_install_all

	# Service files for users to copy or adapt. Nothing here is enabled or
	# installed system-wide: each user opts in with "vigil setup".
	insinto /usr/share/vigil
	doins -r contrib
}

pkg_postinst() {
	elog "vigil is installed but not running. To keep it running, run this"
	elog "as your own user (not as root):"
	elog
	elog "  vigil setup"
	elog
	elog "It shows what it will write and asks first. Service files you can"
	elog "adapt by hand are in ${EPREFIX}/usr/share/vigil/contrib."
}
