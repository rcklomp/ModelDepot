# Failure modes

| what happens | what you see | what to do |
|---|---|---|
| The connection drops mid-download | The job retries with back-off (6 times), resuming each file from where it stopped | Nothing. If it still fails the job shows the reason; Resume continues |
| The container or NAS restarts during a download | The job comes back as "queued: resumed after a restart" and continues from the partial files in `staging/` | Nothing |
| Power loss while storing a download | Manifests, refs and blobs are fsync'd before they get their final name; at worst an unpublished download is redone | Resume the job |
| A shard of a split GGUF is missing upstream | The picker disables that quant ("Incomplete: 1 of 3 parts"); the CLI refuses unless `--allow-incomplete-shards` | Pick another quant |
| The disk is too small | The download is refused before it starts, with the numbers | Free space (Storage), or pick a smaller quant |
| A file's sha256 differs from Hugging Face's | The ingest stops (IntegrityFailed event) and nothing is published | Retry; if it repeats the upstream file changed |
| The data folder is not mounted at start | The server refuses to start and says so; it never creates the store on another disk | Mount the volume, restart |
| A blob is damaged on disk | Storage -> Check integrity lists it (re-hashes every file) | Delete the repo and download it again |
| Garbage collection while a download is being stored | GC stops with "an ingest is running"; it never deletes younger blobs either | Run it again later |
| Deleting a repository while a download is being stored | Refused (409) | Try again when the queue is idle |
| A client asks for a model you do not have | 404, and the model appears in the Library's "Wanted" strip | Click Download |
| An older `huggingface_hub` or a Python 3.8 client | Works if it speaks the plain Hub protocol; set `HF_HUB_DISABLE_XET=1` | |
