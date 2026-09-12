# One-time media and document archive

Run this companion once with a fresh WordPress WXR export before the cleanup project begins. It is deliberately separate from the deployed review and Trash application.

## Start

```powershell
python launch_archive_app.py
```

The launcher opens <http://127.0.0.1:5055>. Leave its terminal window open until the archive reports **Completed**.

1. Select the fresh WordPress XML export.
2. Enter an absolute destination folder. A WSU-approved shared or network drive is preferred when several reviewers need the recovery copy.
3. Inspect the counts, unresolved records, and every source host.
4. Approve only the WordPress or known media/CDN hosts that should receive download requests.
5. Leave credentials blank for the first run. If a planned download from the WordPress host fails with 401 or 403, retry with a dedicated WordPress username and Application Password (created under **Users → Profile → Application Passwords** in WordPress; see "Creating the WordPress Application Password" in [README.md](README.md)). Credentials do not discover file URLs or resolve missing records; they remain in process memory and are not saved.
6. Run the representative test first. It downloads one sample for every media/document file-type and source-host combination, covers every asset role and every record status (so a draft, pending, or private file is proven to download without signing in), and tests every unverified WSU document link. Test files go under `test-downloads/<run-id>/<category>/<type>/` and do not change full-archive progress. Every sample that does not download is listed with its category, file type, host, URL, HTTP or network result, and reason; `test-results.json` and `test-assets.csv` are written even when every sample fails or the test crashes, and a failed test can simply be run again (each run gets a new run id).
7. Review any failed or blocked samples, then start the complete archive. An interrupted or incomplete run can be reopened and retried; already completed files are checked by SHA-256 before being reused.

## Archive contents

Each run creates a timestamped directory containing:

- `manifest.json` — WordPress IDs, source URLs and hosts, local filenames, byte counts, response MIME types, SHA-256 checksums, attempts, and failures.
- `SUMMARY.txt` — a plain-language completion and count summary.
- `records.csv` — the attachment and custom-document inventory, including how each record resolved to files.
- `assets.csv` — one row per planned file with its verification result.
- `failures.csv` — unresolved records and failed or blocked files; it contains only a header when there are none.
- `test-results.json` and `test-assets.csv` — the latest representative test selection and verification results, including each sample's category, file type, record statuses, host, URL, HTTP or network result, and reason.
- `test-downloads/<run-id>/...` from earlier runs can be deleted freely; only the run named in `test-results.json` is referenced.
- `test-downloads/<run-id>/<category>/<type>/...` — files downloaded by the representative test, separate from the complete archive.
- `files/<wordpress-id>/...` — streamed attachment originals and any exported pre-edit image originals.

The temporary WXR upload is removed after the plan is created. The manifest retains only its original filename and SHA-256 checksum. Plan records live in `.archive-data/jobs/`; a record whose archive folder has been deleted is dropped automatically the next time the dashboard loads.

## Completion rules

The run is **Completed** only when every planned asset exists locally and is verified and every exported attachment or `document` custom-post record resolves to at least one asset. Otherwise it remains **Incomplete**.

WSU Document Revisions records are linked to their exported attachment revisions using both the numeric current-revision value and attachment parent IDs. If an exported document has no attachment, its `/documents/YYYY/MM/...` route is treated as a candidate only; it must return a non-HTML file during download or the archive remains incomplete. On gradschool.wsu.edu those candidate routes redirect anonymous requests to `/login`, which the test reports as `HTTP 302 redirect to sign-in page`.

## Stored upload URLs

WP Document Revisions exports each document's attachment with a `/documents/YYYY/MM/name-revision-N.ext` permalink. That route is served through WordPress and answers `403 You are not authorized to access that file` unless the requester is signed in, even for published documents. The stored file itself (`_wp_attached_file`, an MD5-named upload) is served directly by the WSU CDN and is byte-identical to what the public document permalink serves.

The planner therefore infers the uploads base URL from ordinary attachments (for example `https://wpcdn.web.wsu.edu/wp-gradschool/uploads/sites/3121/`) and, for any attachment whose exported URL does not end with its stored path, downloads the stored upload as the primary URL and keeps the exported permalink in `fallback_urls`. The local filename still uses the readable revision name. The fallback is attempted only when the primary fails; credentials are sent to the WordPress host only, never to the CDN. The plan summary shows these as **Stored upload URLs**, and the manifest records them as `stored-upload-url`.

Because the CDN does not apply WordPress visibility rules, draft, pending, and private documents that have a file are archived exactly like published ones and need no sign-in. Only a `document` record with no attachment in the export (empty content and no child attachment) remains unresolved; signing in does not create a file for such a record.

Only original files are in the initial archive scope. Generated WordPress thumbnails and other derivative sizes are not copied; WordPress can usually regenerate them from an original, although plugin-specific crops may require a later full-mirror option.

## Boundaries

- The server binds only to `127.0.0.1` and rejects non-loopback requests.
- Mutating requests require a signed SameSite session and per-session CSRF token.
- Download hosts require explicit approval. The WordPress host and hosts under the same institutional domain (such as the WSU CDN) are pre-checked but remain visible checkboxes; unchecked hosts make their files **blocked** without any request being sent. Redirects to other hosts, redirects to sign-in pages, and private/local network destinations are refused.
- Credentials are sent only to the configured WordPress installation host, never to a separate CDN host, and are never written to the manifest.
- Downloads are streamed through temporary files, capped at 5 GB per file, checked for premature termination, rejected when the body is an HTML page or empty, and renamed only after the file exists with the expected size and a SHA-256 checksum.
- Application Password credentials are only suggested after an actual 401/403 response from the WordPress host. Public files never need them.
- A WXR export inventories WordPress records. It cannot discover stray files present in `wp-content/uploads` that have no database attachment record; a hosting/filesystem backup is required for that separate case.
