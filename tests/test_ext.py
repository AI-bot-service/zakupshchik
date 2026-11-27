import io
import json
import zipfile

from app.web.extension import build_zip
from tests.test_stand import make_client


def test_ext_api_requires_key() -> None:
    client = make_client()
    for url in ("/api/ext/inbox", "/api/ext/deals/1", "/api/ext/lookup?url=https://x"):
        assert client.get(url).status_code == 401, url
        assert client.get(url, headers={"Authorization": "Bearer not-ours"}).status_code == 401, url


def test_extension_pages_require_login() -> None:
    client = make_client()
    for url in ("/extension", "/extension.zip"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_zip_points_to_stand_and_has_tokens() -> None:
    z = zipfile.ZipFile(io.BytesIO(build_zip("https://stand.example")))
    names = set(z.namelist())
    assert {
        "manifest.json",
        "config.js",
        "background.js",
        "sidepanel.html",
        "tokens.css",
        "icons/128.png",
    } <= names
    assert any(n.startswith("fonts/") for n in names)
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["host_permissions"] == ["https://stand.example/*"]
    assert manifest["manifest_version"] == 3
    assert '"https://stand.example"' in z.read("config.js").decode()


def test_package_build_matches_config_and_zip() -> None:
    from app.web.extension import package

    build, files = package("https://stand.example")
    assert len(build) == 16
    assert f'BUILD = "{build}"' in files["config.js"].decode()
    assert "update.js" in files
    z = zipfile.ZipFile(io.BytesIO(build_zip("https://stand.example")))
    assert z.read("config.js") == files["config.js"]
    assert package("https://other.example")[0] != build


def test_package_requires_key() -> None:
    client = make_client()
    assert client.get("/api/ext/package").status_code == 401
