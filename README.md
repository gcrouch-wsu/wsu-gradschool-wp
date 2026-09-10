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

In local mode, the latest report and review decisions remain in memory until the process stops or another export is analyzed. On Vercel, private Blob storage keeps each signed-in reviewer's current audit available across function invocations. Reports are owner-bound and expire after 48 hours when next accessed. Temporary raw XML chunks are deleted after analysis, and abandoned Blob chunks expire after one hour when a later upload refreshes the chunk index.

The dashboard groups records into Posts, Pages, Events, Media, Documents, Forms, TablePress, Factsheets, and Other. Filters, taxonomy drill-downs, evidence details, review decisions, and CSV/JSON exports operate on the stored report for the current session. Published Events and graduate factsheets are marked needs-verification from an assumed public archive; the export cannot prove those archives are enabled or complete. Exported menus are used as entry points only when their status is publish, and even then the export cannot prove a live theme location.

Greg Crouch and `gcrouch` are treated as expected-development author aliases by default. That flag does not replace the underlying finding. Override the comma-separated aliases with `WP_EXPECTED_DEVELOPMENT_AUTHORS`; an empty value disables the rule.

## WordPress REST and Trash

Use a dedicated, least-privilege WordPress account and a revocable Application Password. Opening a record in the review sheet refreshes that record from live WordPress and shows the exported and live identity side by side.

To move records to WordPress Trash, set `WP_REST_WRITE_ENABLED=1`. Vercel additionally requires `WP_REST_REMOTE_WRITE_ENABLED=1` and `VERCEL_ENV=production`; Preview deployments remain read-only. Trash requires an authenticated session and CSRF token, is explicitly confirmed in the browser, is capped at 25 records per request, and never uses WordPress `force` delete. The server re-reads every live record and refuses the write if its identity or version changed. An uncertain DELETE response is never retried; the app reads the record back before deciding whether the move completed.

Store WordPress credentials only as server-side Vercel environment variables. Do not expose them to browser code, reports, exports, logs, Preview deployments, or Git. Each Trash result records the signed-in reviewer and limited action metadata (site, WordPress ID/type, export status, time, and result) in a private immutable Blob object; it does not copy the record title or body. Items can be restored from WordPress Trash until the site's Trash retention removes them. Media may refuse Trash if `MEDIA_TRASH` is disabled; the app will not permanently delete those files.

See [PROJECT_SCOPE.md](PROJECT_SCOPE.md) for definitions, limitations, and the development roadmap.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Vercel deployment procedure and required privacy controls.
