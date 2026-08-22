"""Pure-function tests for the agent's stack/file validation helpers.

These tests must never touch Docker, git or the network: they only exercise
plain string/Path logic from ``agent.docker_manager``.
"""

import pytest

from agent import docker_manager as dm


# ---------------------------------------------------------------------------
# validate_stack_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["myapp", "my-app", "my_app", "my.app", "a1", "A1"])
def test_validate_stack_name_valid(name):
    assert dm.validate_stack_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", None, "My Stack", "myapp!", "-myapp", "myapp..x", "my/app", "my\\app", "..myapp"],
)
def test_validate_stack_name_invalid(name):
    with pytest.raises(ValueError):
        dm.validate_stack_name(name)


# ---------------------------------------------------------------------------
# validate_filename
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    ["docker-compose.yml", ".env", "compose.yaml", "my-file.txt", "a"],
)
def test_validate_filename_valid(filename):
    assert dm.validate_filename(filename) == filename


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "a/b", "a\\b", "a..b"],
)
def test_validate_filename_invalid(filename):
    with pytest.raises(ValueError):
        dm.validate_filename(filename)


# ---------------------------------------------------------------------------
# safe_join
# ---------------------------------------------------------------------------

def test_safe_join_valid_resolves_inside_stack_dir(data_dir):
    base = dm.get_stacks_dir() / "myapp"
    target = dm.safe_join("myapp", "docker-compose.yml")
    assert target == (base / "docker-compose.yml").resolve()
    assert str(target).startswith(str(data_dir.resolve()))


@pytest.mark.parametrize(
    "filename",
    ["../x", "/etc/passwd"],
)
def test_safe_join_traversal_rejected(filename):
    # ``../x`` contains ``..`` and ``/etc/passwd`` contains a path separator;
    # both must be rejected by validate_filename before any resolution.
    with pytest.raises(ValueError):
        dm.safe_join("myapp", filename)


def test_safe_join_invalid_stack_name():
    with pytest.raises(ValueError):
        dm.safe_join("bad stack", "docker-compose.yml")


# ---------------------------------------------------------------------------
# _split_image_reference
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("image_ref", "expected"),
    [
        ("nginx", ("nginx", "latest")),
        ("nginx:1.25", ("nginx", "1.25")),
        ("localhost:5000/repo", ("localhost:5000/repo", "latest")),
        ("localhost:5000/repo:tag", ("localhost:5000/repo", "tag")),
        ("repo@sha256:abc", ("repo@sha256:abc", "")),
        # Registries with a port + digest reference.
        ("localhost:5000/repo@sha256:abc", ("localhost:5000/repo@sha256:abc", "")),
        ("", ("", "latest")),
    ],
)
def test_split_image_reference(image_ref, expected):
    assert dm._split_image_reference(image_ref) == expected


# ---------------------------------------------------------------------------
# _canonical_repository
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("docker.io/library/nginx", "nginx"),
        ("docker.io/org/repo", "org/repo"),
        ("ghcr.io/org/repo", "ghcr.io/org/repo"),
        ("localhost:5000/repo", "localhost:5000/repo"),
        ("nginx", "nginx"),
        ("", ""),
        (None, ""),
    ],
)
def test_canonical_repository(repository, expected):
    assert dm._canonical_repository(repository) == expected


# ---------------------------------------------------------------------------
# _parse_ss_output
# ---------------------------------------------------------------------------

def test_parse_ss_output_empty():
    assert dm._parse_ss_output("") == []
    assert dm._parse_ss_output("\n\n") == []


def test_parse_ss_output_ipv4_and_ipv6_deduplicated_and_sorted():
    output = (
        "LISTEN 0 4096 *:8080 0.0.0.0:* users:((\"docker-proxy\"))\n"
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\"))\n"
        "LISTEN 0 128 [::]:80 [::]:* users:((\"nginx\"))\n"
        "LISTEN 0 4096 *:8080 0.0.0.0:* users:((\"docker-proxy\"))\n"
    )
    assert dm._parse_ss_output(output) == [22, 80, 8080]


def test_parse_ss_output_ignores_invalid_lines():
    output = (
        "LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"
        "garbage line without enough columns\n"
        "LISTEN 0 128 0.0.0.0:notaport 0.0.0.0:*\n"
        "LISTEN 0 128 0.0.0.0 0.0.0.0:*\n"
    )
    assert dm._parse_ss_output(output) == [8080]
