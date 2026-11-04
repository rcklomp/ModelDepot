# The GUI's API (`/api/hub/...`)

JSON over HTTP, served by the same process and port as the Hugging Face-compatible routes (which the AI tools use and the GUI does not).
Auth: `Authorization: Bearer <token>`. **Reads** are open unless `MIRROR_AUTH_TOKEN` is set (then the read token or the admin token is needed).
**Changes** always need the admin token (`MIRROR_ADMIN_TOKEN`, or the one the server made on first start in `<store>/config/admin-token`).
`GET /api/hub/auth/check` never fails: `{admin, read, read_token_required, admin_token_required}` for the token that was sent.
Errors are `{"detail": "<a sentence meant for the user>"}` with 400 (bad request), 401 (token), 404, 409 (busy / not possible now), 502 (upstream).

| endpoint | purpose |
|---|---|
| `GET /status` | `store_id, store_path, free_bytes, total_bytes, stored{blobs,bytes}, models, datasets, queue{running,queued,speed_bps,checks_running} (downloads only; integrity checks are counted apart), hf_token_set, read_token_required, namespace (the organisation a bare upload name gets), read_only, version` |
| `GET /library?type=all\|model\|dataset&q=&sort=name\|size\|updated` | `{items:[{id, type, core_id, commit, files, size_bytes, revisions, updated, source, source_ref, license, formats[]}]}` (`id` is what a client calls it, `org/name`) |
| `GET /repo?type=&id=` | one item plus `refs{}`, `revisions:[{commit, fetched_at, files:[{path,size,sha256}]}]` (newest first), `readme` (text or null) |
| `GET /repo/delete-preview?type=&id=` | `{frees_bytes, shared_bytes}`: bytes only this repo holds / bytes other repos also use |
| `POST /repo/delete` `{type,id}` (admin) | `{blobs_deleted, freed_bytes, shared_bytes_kept}`; 409 while a download is being stored |
| `GET /search?source=hf\|civitai&type=model\|dataset&q=&sort=&filter=&nsfw=&limit=` | `{results:[{source,type,id,author,downloads,likes,updated,gated,formats[],task,url,in_library}]}` (Civitai: `id` is the version id, plus `name, version, base_model, civitai_type, size, nsfw`) |
| `GET /upstream/files?source=&type=&id=&revision=` | the picker: `{commit, gated, license, in_library, total_size, file_count, groups:[{key,title,exclusive,options:[{id,label,size,file_count,files[],note,shards?,complete?}]}], quick:[option], extras:option, files:[{path,size}]}`; an option with `complete:false` must not be selectable |
| `GET /jobs` | `{jobs:[Job], summary{running,queued,speed_bps}, window, max_concurrent}` |
| `POST /jobs` (admin) | body `{source:"hf"\|"civitai", id, type, revision, files:[paths]\|null, total_bytes, file_count}` -> `{job, already_queued}`; 400 explains (bad id, not enough free space) |
| `POST /jobs/{id}/pause\|resume\|remove` (admin) | pause keeps the partial download; remove discards it |
| `POST /ops/verify` `{type?,id?}` (admin) | starts a re-hash of the whole library or one repo as a job |
| `POST /ops/gc` `{dry_run}` (admin) | `{dry_run, blobs, checked, bytes}` (bytes only on a dry run) |
| `GET /wanted` / `POST /wanted/dismiss` `{type,id}` | what clients asked this server for and it did not have |
| `GET /settings` / `PUT /settings` (admin) | `{hf_token_set, civitai_token_set, *_from_env, max_concurrent, download_window "HH:MM-HH:MM"\|"", admin_token_file}`; tokens are write-only |

**Job**: `{id, kind:"download"\|"verify", label, status: queued\|running\|paused\|done\|failed, phase: resolving\|downloading\|storing\|hashing, created, started, finished,
bytes_total, bytes_done, speed_bps, eta_s, files_total, files_done, message, note, result, log[], spec}`. A queued job's `note` says why it waits (download window, another download).

## Web upload (`/api/hub/upload/...`, all admin)

For the browser's upload screen. Files are uploaded one request each into a **session** (a name the client makes up: 1-64 letters, digits, `-`, `_`), then the
session is published as ONE new revision of `main`. `MIRROR_READ_ONLY` makes all three answer 403. Errors are `{"detail": ...}` as everywhere in `/api/hub`.

| endpoint | purpose |
|---|---|
| `PUT /upload/file?type=model\|dataset&id=org/name&path=dir/file.ext&session=<name>` (admin) | the request body IS the file (send the `File` object itself); streamed to disk, never held in memory, no size limit. `{path, size}`. The same `path` again replaces the file. `id` without a `/` gets the server's namespace (`MIRROR_NAMESPACE`). The repository need not exist yet. 400: bad `path` (absolute, `..`, `.`, empty segment, backslash, control character, **any segment starting with `.`**) or bad `type`/`id`/`session`; 409: the session already holds files for another repository, or `path` collides with another file as a file/folder |
| `POST /upload/commit` `{type, id, session, summary?, replace?}` (admin) | creates the repository if it is new, then publishes every file of the session as one new revision: the current files plus the session's (same path = overwritten). `replace:true` makes the session the whole new content (files not in it are removed). A session that changes nothing publishes nothing. Removes the session on success. `{id, type, commit, created, files}` (`created:false` = nothing changed, `commit` is the head). 404: unknown/finished session; 400: empty session; 409: the session belongs to another repository |
| `POST /upload/discard` `{session}` (admin) | deletes the session's files; always `{ok:true}` |

A browser folder pick should skip dot-files (`.DS_Store`, `.git/`): they are refused. Uploaded sessions nobody commits or discards are swept after 7 days.
