"""Flexible uploads (B22): a lone .py, .zip, .tar.gz/.tgz/.tar, and /jobs/inspect."""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import uuid
import zipfile

from app.archive import candidate_entrypoints, suggest_entrypoint
from app.storage import storage
from harness.jobs import Portal


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _tar(files: dict[str, str], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def _submit(client, portal: Portal, filename: str, data: bytes, **form):
    body = {
        "project_id": portal.ensure_project(),
        "environment_id": portal.environment_id(),
        "resources": json.dumps({"gpu_class": f"up-{uuid.uuid4().hex[:8]}", "max_runtime_seconds": 60}),
        **form,
    }
    return client.post(
        "/jobs", data=body, files={"archive": (filename, data, "application/octet-stream")}, headers=portal.headers
    )


def _inspect(client, portal: Portal, filename: str, data: bytes):
    return client.post(
        "/jobs/inspect", files={"archive": (filename, data, "application/octet-stream")}, headers=portal.headers
    )


def _stored(job: dict) -> tuple[str, bytes]:
    """(object key, bytes) of the upload stored for a job."""
    keys = storage.list_keys(f"submissions/{job['id']}/")
    assert len(keys) == 1, keys
    return keys[0], storage.get_bytes(keys[0])


def test_a_single_py_file_is_wrapped_and_is_its_own_entrypoint(client):
    resp = _submit(client, Portal(client), "solo.py", b"print('hi')\n")
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["entrypoint"] == "solo.py"
    key, data = _stored(job)
    assert key.endswith("input.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        assert tf.getnames() == ["solo.py"]
        assert tf.extractfile("solo.py").read() == b"print('hi')\n"


def test_a_py_file_path_is_reduced_to_its_name(client):
    resp = _submit(client, Portal(client), "C:\\Users\\me\\My Code\\run it.py", b"x = 1\n")
    assert resp.status_code == 201, resp.text
    assert resp.json()["entrypoint"] == "run_it.py"


def test_a_zip_is_accepted_and_the_entrypoint_found_inside_a_folder(client):
    data = _zip({"proj/src/train.py": "print(1)", "proj/requirements.txt": "numpy\n"})
    resp = _submit(client, Portal(client), "proj.zip", data, entrypoint="src/train.py")
    assert resp.status_code == 201, resp.text
    assert resp.json()["entrypoint"] == "proj/src/train.py"
    key, stored = _stored(resp.json())
    assert key.endswith("input.zip") and stored == data


def test_tar_gz_tgz_and_plain_tar_are_accepted(client):
    portal = Portal(client)
    files = {"train.py": "print(1)"}
    for filename, data, suffix in (
        ("a.tar.gz", _tar(files), "input.tar.gz"),
        ("a.tgz", _tar(files), "input.tar.gz"),
        ("a.tar", _tar(files, "w"), "input.tar"),
    ):
        resp = _submit(client, portal, filename, data, entrypoint="train.py")
        assert resp.status_code == 201, (filename, resp.text)
        assert resp.json()["entrypoint"] == "train.py"
        assert _stored(resp.json())[0].endswith(suffix)


def test_the_entrypoint_may_be_left_out_and_the_likeliest_script_is_used(client):
    data = _zip({"utils.py": "", "scripts/main.py": "", "train.py": "", "README.md": "x"})
    resp = _submit(client, Portal(client), "p.zip", data)
    assert resp.status_code == 201, resp.text
    assert resp.json()["entrypoint"] == "train.py"


def test_bad_uploads_are_refused(client):
    portal = Portal(client)
    assert _submit(client, portal, "notes.txt", b"hello").status_code == 400
    assert _submit(client, portal, "broken.zip", b"PK\x03\x04 not really").status_code == 400
    assert _submit(client, portal, "broken.tar.gz", b"\x1f\x8b garbage").status_code == 400
    empty = _submit(client, portal, "empty.zip", _zip({}))
    assert empty.status_code == 400
    no_py = _submit(client, portal, "data.zip", _zip({"data.csv": "1,2"}))
    assert no_py.status_code == 400 and "no .py" in no_py.text
    slip = _submit(client, portal, "slip.zip", _zip({"../evil.py": "x"}), entrypoint="evil.py")
    assert slip.status_code == 400 and "unsafe" in slip.text
    assert _submit(client, portal, "t.zip", _zip({"t.py": ""}), entrypoint="x.sh").status_code == 400
    assert _submit(client, portal, "t.zip", _zip({"t.py": ""}), entrypoint="other.py").status_code == 400


def test_inspect_lists_scripts_suggests_one_and_reads_requirements(client):
    portal = Portal(client)
    files = {
        "requirements.txt": "torch\nnumpy==1.26\n",
        "src/main.py": "",
        "src/__init__.py": "",
        "tools/helper.py": "",
        "__MACOSX/._main.py": "",
        ".hidden/x.py": "",
        "venv/lib/site-packages/a.py": "",
        "data.csv": "1",
    }
    for filename, data in (("p.zip", _zip(files)), ("p.tar.gz", _tar(files)), ("p.tar", _tar(files, "w"))):
        resp = _inspect(client, portal, filename, data)
        assert resp.status_code == 200, (filename, resp.text)
        body = resp.json()
        assert body["entrypoints"] == ["src/main.py", "tools/helper.py"]
        assert body["suggested_entrypoint"] == "src/main.py"
        assert body["requirements"] == "torch\nnumpy==1.26"


def test_inspect_finds_requirements_next_to_the_entrypoint_or_in_the_project_folder(client):
    portal = Portal(client)
    beside = _zip({"proj/exp/train.py": "", "proj/exp/requirements.txt": "scipy\n", "proj/requirements.txt": "other"})
    assert _inspect(client, portal, "a.zip", beside).json()["requirements"] == "scipy"
    wrapped = _zip({"proj/train.py": "", "proj/requirements.txt": "pandas\n"})
    body = _inspect(client, portal, "b.zip", wrapped).json()
    assert body["suggested_entrypoint"] == "proj/train.py" and body["requirements"] == "pandas"
    assert _inspect(client, portal, "c.zip", _zip({"train.py": ""})).json()["requirements"] is None
    assert _inspect(client, portal, "d.zip", _zip({"train.py": "", "requirements.txt": " \n"})).json()[
        "requirements"
    ] is None


def test_inspect_of_a_single_script(client):
    body = _inspect(client, Portal(client), "go.py", b"print(1)").json()
    assert body == {"entrypoints": ["go.py"], "suggested_entrypoint": "go.py", "requirements": None}


def test_inspect_refuses_what_submit_would_and_needs_a_sign_in(client):
    portal = Portal(client)
    assert _inspect(client, portal, "x.txt", b"hello").status_code == 400
    assert _inspect(client, portal, "x.zip", b"").status_code == 400
    assert client.post("/jobs/inspect", files={"archive": ("a.zip", _zip({"a.py": ""}))}).status_code == 401


def test_dot_slash_names_are_cleaned_for_entrypoints():
    names = ["./train.py", "./sub/b.py", "./sub/__init__.py"]
    from app.archive import _clean

    assert candidate_entrypoints([_clean(n) for n in names]) == ["train.py", "sub/b.py"]


def test_suggestion_prefers_known_names_then_shallow_paths():
    assert suggest_entrypoint(["a.py", "b/train.py"]) == "b/train.py"
    assert suggest_entrypoint(["z.py", "deep/er/a.py"]) == "z.py"
    assert suggest_entrypoint(["x/main.py", "train.py", "run.py"]) == "train.py"
    assert suggest_entrypoint([]) is None


def test_gzip_magic_decides_even_when_the_name_lies(client):
    data = gzip.compress(_tar({"train.py": "print(1)"}, "w"))
    resp = _submit(client, Portal(client), "weird.bin", data, entrypoint="train.py")
    assert resp.status_code == 201, resp.text
