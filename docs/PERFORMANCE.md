# Performance

**Not measured on the DS713+ yet.** Facts and expectations, kept apart:

- Measured (2026-11-04, a Mac, local loopback, the application's own test files): the end-to-end behaviour is correct; no throughput figure from it is meaningful for a NAS.
- Known cost: every downloaded file is read once to hash it (sha256), then moved into the store; on an Atom D2700 and a spinning disk that pass is the slow step of an ingest
  (a 100 GB model: expect tens of minutes, disk- or CPU-bound; not measured).
- Serving: files are streamed in 1 MiB chunks by pure-Python uvicorn (no uvloop). Whether that saturates the NAS's gigabit port is not measured; the first thing to measure
  on the NAS is `hf download` of a large file from another machine (`curl -o /dev/null http://<nas>:8000/<org>/<name>/resolve/main/<big file>` shows the rate).
- Memory: one server process plus, during a download, one worker process; both small (Python, FastAPI, huggingface_hub); no database.
