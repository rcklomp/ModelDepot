"""Datasets through the real client: dataset_info, snapshot_download, `hf download --repo-type dataset`, `datasets.load_dataset`."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .conftest import COMMIT, FILES, stage_repo

DS_FILES = {
    "README.md": b"# tiny dataset\n",
    "data/train.jsonl": b'{"text": "a", "label": 0}\n{"text": "b", "label": 1}\n{"text": "c", "label": 0}\n',
    "data/test.jsonl": b'{"text": "d", "label": 1}\n',
}
DS = "datasets/acme/tiny-ds"          # the core id; clients call it acme/tiny-ds with repo_type="dataset"


def test_dataset_info_snapshot_and_tree(make_live):
    live = make_live()
    stage_repo(live.repo, DS, DS_FILES)
    code = ("import json, os\nfrom huggingface_hub import HfApi, snapshot_download\na = HfApi()\n"
            "i = a.dataset_info('acme/tiny-ds')\nprint(i.id, i.sha)\n"
            "print(sorted(e.path for e in a.list_repo_tree('acme/tiny-ds', repo_type='dataset', recursive=True)))\n"
            "p = snapshot_download('acme/tiny-ds', repo_type='dataset')\n"
            "print(sorted(os.path.relpath(os.path.join(r, f), p) for r, _, fs in os.walk(p) for f in fs))\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-800:]
    lines = r.stdout.strip().splitlines()
    assert lines[0] == f"acme/tiny-ds {COMMIT}"
    assert "data" in lines[1] and "data/train.jsonl" in lines[1]
    assert sorted(eval(lines[2])) == sorted(DS_FILES)


def test_hf_cli_download_dataset(make_live, tmp_path):
    live = make_live()
    stage_repo(live.repo, DS, DS_FILES)
    hf = Path(sys.executable).parent / "hf"
    out = tmp_path / "out"
    r = live.client(f"import subprocess, sys\nr = subprocess.run(['{hf}', 'download', 'acme/tiny-ds', '--repo-type', 'dataset', "
                    f"'--local-dir', '{out}'], capture_output=True, text=True)\nprint(r.returncode); print(r.stderr[-600:])\n")
    assert r.stdout.strip().splitlines()[0] == "0", r.stdout + r.stderr
    assert (out / "data" / "train.jsonl").read_bytes() == DS_FILES["data/train.jsonl"]


def test_load_dataset(make_live):
    """The call the owner asked for by name."""
    live = make_live()
    stage_repo(live.repo, DS, DS_FILES)
    code = ("from datasets import load_dataset\nds = load_dataset('acme/tiny-ds')\n"
            "print(sorted(ds.keys()))\nprint(ds['train'].num_rows, ds['test'].num_rows)\nprint(ds['train'][1])\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-1200:]
    lines = r.stdout.strip().splitlines()[-3:]
    assert lines[0] == "['test', 'train']" and lines[1] == "3 1" and "'text': 'b'" in lines[2]


def test_legacy_single_segment_names_work_for_datasets_and_models(make_live):
    live = make_live()
    stage_repo(live.repo, "datasets/_user/squad", {"train.jsonl": b'{"q": 1}\n'})
    stage_repo(live.repo, "gpt2", {"config.json": b'{"n_layer": 12}'})
    code = ("from huggingface_hub import hf_hub_download\n"
            "print(open(hf_hub_download('squad', 'train.jsonl', repo_type='dataset')).read().strip())\n"
            "print(open(hf_hub_download('gpt2', 'config.json')).read())\n")
    r = live.client(code)
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip().splitlines() == ['{"q": 1}', '{"n_layer": 12}']


def test_a_model_and_a_dataset_with_the_same_name_do_not_collide(make_live):
    live = make_live()
    stage_repo(live.repo, "acme/x", {"f.txt": b"I am the model"}, commit="a" * 40)
    stage_repo(live.repo, "datasets/acme/x", {"f.txt": b"I am the dataset"}, commit="b" * 40)
    code = ("from huggingface_hub import hf_hub_download\n"
            "print(open(hf_hub_download('acme/x', 'f.txt')).read())\n"
            "print(open(hf_hub_download('acme/x', 'f.txt', repo_type='dataset')).read())\n")
    r = live.client(code)
    assert r.stdout.strip().splitlines() == ["I am the model", "I am the dataset"], r.stderr[-500:]


def test_the_read_token_protects_datasets_too(make_live):
    live = make_live(auth_token="s3cret")
    stage_repo(live.repo, DS, DS_FILES)
    for path in ("/api/datasets/acme/tiny-ds", "/datasets/acme/tiny-ds/resolve/main/README.md"):
        try:
            urllib.request.urlopen(live.url + path, timeout=10)
            raise AssertionError(path + " was served without a token")
        except urllib.error.HTTPError as e:
            assert e.code == 401
