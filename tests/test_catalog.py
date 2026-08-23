from __future__ import annotations

from app.catalog import PYTORCH_IMAGE


def test_pytorch_catalog_is_pinned(client):
    resp = client.get("/environments", headers={"X-Dev-User": "alice@uni.edu"})
    assert resp.status_code == 200
    names = {row["name"]: row for row in resp.json()["environments"]}
    assert names["pytorch-stable-cu124"]["image_digest"] == PYTORCH_IMAGE
