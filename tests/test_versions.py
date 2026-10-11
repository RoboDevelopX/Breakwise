from breakwise import versions
from breakwise.models import Ecosystem

PY, NPM = Ecosystem.PYPI, Ecosystem.NPM


def test_pep440_and_semver_ordering():
    assert versions.latest_stable(["1.0", "2.0rc1", "1.10"], PY) == "1.10"
    assert versions.latest_stable(["4.18.2", "5.0.0-beta.3", "4.21.0"], NPM) == "4.21.0"
    assert versions.releases_between(["1.0", "1.1", "2.0b1", "2.0", "2.1"], "1.0", "2.0", PY) == ["1.1", "2.0"]
    assert versions.same("2.0", "2.0.0", PY)
    assert versions.parse("5.0.0-beta.10", NPM) > versions.parse("5.0.0-beta.9", NPM)


def test_npm_range_floor():
    assert versions.npm_range_floor("^4.18.2") == "4.18.2"
    assert versions.npm_range_floor("~1.2") == "1.2.0"
    assert versions.npm_range_floor("1.x") == "1.0.0"
    assert versions.npm_range_floor(">=2.1.0 <3") == "2.1.0"
    assert versions.npm_range_floor("*") is None
    assert versions.npm_range_floor("github:user/repo") is None


def test_find_version_in_headings():
    assert versions.find_version("## v2.14.0 (2026-10-08)") == "2.14.0"
    assert versions.find_version("Version 3.1.3") == "3.1.3"
    assert versions.find_version("## [1.2.0] - 2024-01-01") == "1.2.0"
    assert versions.find_version("Unreleased") is None
