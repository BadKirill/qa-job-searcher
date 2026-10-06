import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings

PDF = Path(__file__).parent / "fixtures" / "candidate.pdf"


@pytest.fixture
def client(tmp_path):
    app = create_app(WebSettings(tmp_path / "private", ("http://testserver",), tmp_path / "dist"))
    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        token = app.state.security.setup_file.read_text()
        setup = client.post("/api/setup", json={"token": token, "password": "test-pdf-password"})
        client.headers["x-csrf-token"] = setup.json()["csrf_token"]
        yield client


def upload(client):
    response = client.post(
        "/api/template-drafts", files={"file": ("candidate.pdf", PDF.read_bytes())}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_pdf_original_preview_rereading_preserves_active_and_explicit_edits(client):
    template = upload(client)
    source = f"/api/templates/{template['id']}"
    assert "pages" not in template["reference"]
    assert "path" not in template and "style" not in template
    content = client.get(source + "/source")
    assert hashlib.sha256(content.content).hexdigest() == template["reference"]["sha256"]
    assert content.headers["content-disposition"].startswith("attachment;")
    for page in (1, 2):
        response = client.get(source + f"/pages/{page}")
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    facts = template["facts"]
    facts[0]["kind"] = "body"
    updated = client.patch(
        f"/api/template-drafts/{template['id']}",
        json={
            "facts": facts,
            "instructions": "Keep the full experience section",
        },
    )
    assert updated.json()["facts"][0]["kind"] == "body"
    assert (
        client.post(
            f"/api/template-drafts/{template['id']}/activate",
            json={
                "expected_version": None,
                "confirmed": True,
            },
        ).status_code
        == 200
    )
    reread = client.post(source + "/reparse", json={}).json()
    assert reread["id"] != template["id"] and reread["status"] == "draft"
    assert reread["instructions"] == "Keep the full experience section"
    assert reread["reference"] == template["reference"]
    assert client.get("/api/profile").json()["active"]["id"] == template["id"]
    cleared = client.patch(
        f"/api/template-drafts/{reread['id']}",
        json={
            "facts": reread["facts"],
            "instructions": "",
        },
    )
    assert cleared.json()["instructions"] == ""
    assert client.post("/api/logout").status_code == 200
    for suffix in ("/source", "/pages/1"):
        assert client.get(source + suffix).status_code == 401


@pytest.mark.parametrize("page", [0, -1, 3, 999999])
def test_pdf_page_bounds_and_missing_template(client, page):
    template = upload(client)
    assert client.get(f"/api/templates/{template['id']}/pages/{page}").status_code == 404
    assert client.get("/api/templates/missing/source").status_code == 404


def test_pdf_source_hash_path_size_and_reparse_csrf(client, tmp_path):
    template = upload(client)
    store = client.app.state.store
    source = f"/api/templates/{template['id']}"
    assert client.post(source + "/reparse", headers={"x-csrf-token": ""}).status_code == 403
    raw = store.template(template["id"])
    original = Path(raw["path"])
    original.write_bytes(b"changed")
    assert client.get(source + "/source").status_code == 409
    assert client.post(source + "/reparse").status_code == 409
    original.write_bytes(PDF.read_bytes())
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF.read_bytes())
    store.update_template(template["id"], {"path": str(outside)})
    assert client.get(source + "/source").status_code == 404
    store.update_template(template["id"], {"path": str(original)})
    with original.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    assert client.get(source + "/source").status_code == 422
    original.unlink()
    assert client.get(source + "/source").status_code == 404


def test_docx_rereading_is_explicitly_rejected_and_legacy_pdf_is_supported(client):
    template = upload(client)
    store = client.app.state.store
    raw = store.template(template["id"])
    store.update_template(template["id"], {"reference": None, "source_sha256": None})
    assert client.post(f"/api/templates/{template['id']}/reparse").status_code == 201
    path = Path(raw["path"])
    docx = path.with_suffix(".docx")
    path.rename(docx)
    store.update_template(
        template["id"], {"path": str(docx), "source_sha256": raw["source_sha256"]}
    )
    assert client.post(f"/api/templates/{template['id']}/reparse").status_code == 422
