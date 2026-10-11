import json

from breakwise import manifests
from breakwise.models import Ecosystem


def test_requirements_pins_ranges_and_includes(tmp_path):
    (tmp_path / "base.txt").write_text("requests==2.31.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text(
        "-r base.txt\n"
        "# a comment\n"
        "Flask[async] == 3.0.0  # trailing comment\n"
        "urllib3>=1.26,<2\n"
        "numpy~=1.26.0 ; python_version >= '3.9'\n"
        "-e git+https://github.com/x/y.git#egg=y\n"
        "pkg-with-hash==1.0 \\\n    --hash=sha256:abc\n"
        "unpinned-thing\n"
    )
    deps = {d.name: d for d in manifests.parse_file(req, tmp_path, use_installed=False)}
    assert deps["requests"].current == "2.31.0" and deps["requests"].current_source == "pinned"
    assert deps["flask"].current == "3.0.0"
    assert deps["urllib3"].current == "1.26" and deps["urllib3"].current_source == "lower-bound"
    assert deps["numpy"].current == "1.26.0"
    assert deps["pkg-with-hash"].current == "1.0"
    assert deps["unpinned-thing"].current is None
    assert "y" not in deps


def test_pyproject_with_uv_lock(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nrequires-python=">=3.10"\ndependencies=["httpx>=0.25", "rich"]\n'
        '[project.optional-dependencies]\ndev=["pytest==8.0.0"]\n'
    )
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "httpx"\nversion = "0.27.0"\n')
    deps = {d.name: d for d in manifests.parse_file(tmp_path / "pyproject.toml", tmp_path, use_installed=False)}
    assert deps["httpx"].current == "0.27.0" and deps["httpx"].current_source == "lockfile"
    assert deps["pytest"].current == "8.0.0"
    assert deps["rich"].current is None
    assert manifests.requires_python(tmp_path) == "3.10"


def test_poetry_dependencies(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython="^3.11"\ndjango="^4.2.1"\nrequests={version="2.31.0", extras=["socks"]}\n'
    )
    deps = {d.name: d for d in manifests.parse_file(tmp_path / "pyproject.toml", tmp_path, use_installed=False)}
    assert "python" not in deps
    assert deps["django"].current == "4.2.1"
    assert deps["requests"].current == "2.31.0"


def test_package_json_uses_lockfile(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"express": "^4.18.2", "react": "18.2.0", "local": "file:../local"},
        "devDependencies": {"jest": "~29.1.0", "@types/node": ">=20 <21"},
        "engines": {"node": ">=18"},
    }))
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {}, "node_modules/express": {"version": "4.19.2"},
                     "node_modules/foo/node_modules/express": {"version": "3.0.0"}},
    }))
    deps = {d.name: d for d in manifests.parse_file(tmp_path / "package.json", tmp_path)}
    assert deps["express"].current == "4.19.2" and deps["express"].current_source == "lockfile"
    assert deps["react"].current == "18.2.0" and deps["react"].current_source == "pinned"
    assert deps["jest"].current == "29.1.0"
    assert deps["@types/node"].current == "20.0.0"
    assert deps["local"].current is None
    assert all(d.ecosystem == Ecosystem.NPM for d in deps.values())
    assert manifests.node_engine(tmp_path) == "18"


def test_discover_skips_vendored_dirs(tmp_path):
    (tmp_path / "requirements.txt").write_text("a==1\n")
    (tmp_path / "requirements-dev.txt").write_text("b==1\n")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text("{}")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    names = [p.relative_to(tmp_path).as_posix() for p in manifests.discover(tmp_path)]
    assert names == ["requirements-dev.txt", "requirements.txt", "web/package.json"]
