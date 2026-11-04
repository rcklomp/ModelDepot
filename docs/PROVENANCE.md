# Provenance

Every published revision has a `manifest.json` (schema version 1, written once, never edited):

- `repo_id`, and `source`: `kind` (`huggingface`, `civitai`, `local`) and `ref` (the upstream URL or the local path it came from);
- `revision`: the upstream commit (a 40-hex Hugging Face commit; a synthetic id for Civitai and local imports), `fetched_at` (UTC), the license name if known;
- `files[]`: `path`, `size`, `blob` (`sha256:<hex>`, the identity of the content), `checksums.sha256`, and `metadata.hf` (the upstream etag and commit, echoed back so
  clients see what Hugging Face would send).

The GUI shows source, commit and each file's sha256 on the repository page. `events.jsonl` records what happened and when (blob stored, revision published, ref updated,
integrity failed, repository deleted, garbage collected). A Hugging Face file with a published sha256 is verified against it before it is stored; the file is hashed once.
