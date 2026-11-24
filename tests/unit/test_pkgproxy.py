import hashlib

import httpx
import pytest

from fixloop_pkgproxy.main import JSON_TYPE, create_app

WHEEL = b"PK\x03\x04 fake wheel bytes"
SHA = hashlib.sha256(WHEEL).hexdigest()


def upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/simple/pytest/":
        return httpx.Response(
            200,
            json={
                "meta": {"api-version": "1.1"},
                "name": "pytest",
                "files": [
                    {
                        "filename": "pytest-9.0.0-py3-none-any.whl",
                        "url": "https://files.test/pytest-9.0.0-py3-none-any.whl",
                        "hashes": {"sha256": SHA},
                        "requires-python": ">=3.10",
                        "core-metadata": {"sha256": "ab"},
                    },
                    {
                        "filename": "pytest-9.0.0.tar.gz",
                        "url": "https://files.test/pytest-9.0.0.tar.gz",
                        "hashes": {"sha256": "00"},
                    },
                    {
                        "filename": "pytest-8.0.0-py3-none-any.whl",
                        "url": "https://files.test/bad.whl",
                        "hashes": {"sha256": "0" * 64},
                    },
                ],
            },
        )
    if request.url.path == "/pytest-9.0.0-py3-none-any.whl":
        return httpx.Response(200, content=WHEEL)
    if request.url.path == "/bad.whl":
        return httpx.Response(200, content=b"tampered")
    return httpx.Response(404)


@pytest.fixture
def client(tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text("pytest\n# comment\nRequests\n")
    app = create_app("https://pypi.test", allow, tmp_path / "cache", transport=httpx.MockTransport(upstream))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_only_allowlisted_projects(client):
    assert (await client.get("/simple/django/")).status_code == 404
    index = await client.get("/simple/", headers={"Accept": JSON_TYPE})
    assert {p["name"] for p in index.json()["projects"]} == {"pytest", "requests"}


async def test_wheels_only_with_hashes(client):
    page = await client.get("/simple/PyTest/", headers={"Accept": JSON_TYPE})
    files = page.json()["files"]
    assert [f["filename"] for f in files] == [
        "pytest-9.0.0-py3-none-any.whl",
        "pytest-8.0.0-py3-none-any.whl",
    ]
    assert files[0]["url"] == "/files/pytest/pytest-9.0.0-py3-none-any.whl" and files[0]["core-metadata"]
    html = await client.get("/simple/pytest/")
    assert f"#sha256={SHA}" in html.text and ".tar.gz" not in html.text


async def test_downloads_are_hash_checked_and_cached(client):
    good = await client.get("/files/pytest/pytest-9.0.0-py3-none-any.whl")
    assert good.status_code == 200 and good.content == WHEEL
    again = await client.get("/files/pytest/pytest-9.0.0-py3-none-any.whl")
    assert again.content == WHEEL
    tampered = await client.get("/files/pytest/pytest-8.0.0-py3-none-any.whl")
    assert tampered.status_code == 502
    assert (await client.get("/files/pytest/pytest-9.0.0.tar.gz")).status_code == 404
