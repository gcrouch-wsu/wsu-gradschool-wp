# WSU Graduate School WordPress Orphan Analyzer

Private Flask application for analyzing a WordPress WXR export and moving reviewed records to WordPress Trash. It inventories content and media, extracts references, builds a link graph, and presents explainable orphan candidates for human review. It runs locally in memory or on Vercel with private Blob-backed audit sessions.

## Configure the three reviewers

The application fails closed until `APP_AUTH_USERS_JSON` contains exact `@wsu.edu` email addresses mapped to password hashes. Choose each content-audit password locally; do not reuse a WSU Network ID password and never store plaintext passwords.

Generate a hash locally for each reviewer:

```powershell
python -c "import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass.getpass('Content-audit password: ')))"
```

Build one JSON object such as `{"reviewer@wsu.edu":"scrypt:..."}` and set it as `APP_AUTH_USERS_JSON` in `.env.local` and the Vercel Production environment. `FLASK_SECRET_KEY` must also be stable and secret. Changing either the user hash or Flask secret signs users out; there is intentionally no public registration or password-reset flow.

## Run locally

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>, select **Tools → Export → All content** from WordPress, and upload the resulting XML file.

## One-time local media archive

Before beginning a cleanup project, run `python launch_archive_app.py` and open <http://127.0.0.1:5055>. Choose the same fresh WXR export and an absolute local or WSU-approved shared-drive destination. The companion inventories attachment files and `document` custom-post records, shows every source host for explicit approval, and then streams files into a resumable archive with SHA-256 checksums. Run the bounded representative test before the complete archive; it covers every media/document file-type and source-host combination, every asset role, every record status (draft, pending, and private files included), and every unverified document link without changing full-archive progress, and it lists each sample that did not download with its host, URL, HTTP result, and reason. No WordPress sign-in is needed for public or unpublished files: WP Document Revisions permalinks are replaced by the stored upload on the WSU CDN, which serves files regardless of post status. Application Password credentials are only for retrying an actual 401/403 from the WordPress host and remain in memory for that run.

See [ARCHIVING.md](ARCHIVING.md) for the archive contents, completion rules, recovery workflow, and known boundaries.

An archive is marked **Completed** only when every planned file is present and verified and every attachment or `document` record resolves to at least one file. Failed downloads, unapproved hosts, attachment records without URLs, and custom document records whose file relationship is absent from the WXR keep the archive **Incomplete**. Generated thumbnails are not downloaded; current attachment originals and exported WordPress pre-edit originals are included.

In local mode, the latest report and review decisions remain in memory until the process stops or another export is analyzed. On Vercel, private Blob storage keeps each signed-in reviewer's current audit available across function invocations. Reports are owner-bound and expire after 48 hours when next accessed. Temporary raw XML chunks are deleted after analysis, and abandoned Blob chunks expire after one hour when a later upload refreshes the chunk index.

The dashboard groups records into Posts, Pages, Events, Media, Documents, Forms, TablePress, Factsheets, and Other. Filters, taxonomy drill-downs, evidence details, review decisions, and CSV/JSON exports operate on the stored report for the current session. Published Events and graduate factsheets are marked needs-verification from an assumed public archive; the export cannot prove those archives are enabled or complete. Exported menus are used as entry points only when their status is publish, and even then the export cannot prove a live theme location.

Greg Crouch and `gcrouch` are treated as expected-development author aliases by default. That flag does not replace the underlying finding. Override the comma-separated aliases with `WP_EXPECTED_DEVELOPMENT_AUTHORS`; an empty value disables the rule.

## WordPress REST and Trash

Use a dedicated, least-privilege WordPress account and a revocable Application Password. Opening a record in the review sheet refreshes that record from live WordPress and shows the exported and live identity side by side.

To move records to WordPress Trash, set `WP_REST_WRITE_ENABLED=1`. Vercel additionally requires `WP_REST_REMOTE_WRITE_ENABLED=1` and `VERCEL_ENV=production`; Preview deployments remain read-only. For one record, open its review card, wait for the live check, choose **Approve to delete**, then choose **Move to Trash**. For a bulk action, select up to 25 REST-supported rows without approving each one first, choose **Review selected for Trash**, inspect the fresh WordPress preflight results, and confirm the ready records. Trash requires an authenticated session and CSRF token and never uses WordPress `force` delete. The server re-reads every live record and refuses the write if its identity or version changed. An uncertain DELETE response is never retried; the app reads the record back before deciding whether the move completed.

Store WordPress credentials only as server-side Vercel environment variables. Do not expose them to browser code, reports, exports, logs, Preview deployments, or Git. Each Trash result records the signed-in reviewer and limited action metadata (site, WordPress ID/type, export status, time, and result) in a private immutable Blob object; it does not copy the record title or body. Items can be restored from WordPress Trash until the site's Trash retention removes them. Media may refuse Trash if `MEDIA_TRASH` is disabled; the app will not permanently delete those files.

See [PROJECT_SCOPE.md](PROJECT_SCOPE.md) for definitions, limitations, and the development roadmap.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Vercel deployment procedure and required privacy controls.
