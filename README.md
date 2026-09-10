# WSU Graduate School WordPress Orphan Analyzer

Flask application for analyzing a WordPress WXR export. It inventories content and media, extracts references, builds a link graph, and presents explainable orphan candidates for human review. It runs locally in memory or on Vercel with private Blob-backed audit sessions.

## Run locally

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>, select **Tools → Export → All content** from WordPress, and upload the resulting XML file.

In local mode, the latest report and review decisions remain in memory until the process stops or another export is analyzed. On Vercel, private Blob storage keeps the report available across function invocations; temporary raw XML chunks are deleted after analysis and abandoned Blob chunks expire after one hour when a later upload records them. Do not put `WP_REST_*` variables on Vercel; the app also refuses to start REST from that environment, but operators must still keep those secrets out of the Vercel project.

The dashboard groups records into Posts, Pages, Events, Media, Documents, Forms, TablePress, Factsheets, and Other. Filters, taxonomy drill-downs, evidence details, review decisions, and CSV/JSON exports operate on the stored report for the current session. Published Events and graduate factsheets are marked needs-verification from an assumed public archive; the export cannot prove those archives are enabled or complete. Exported menus are used as entry points only when their status is publish, and even then the export cannot prove a live theme location.

Greg Crouch and `gcrouch` are treated as expected-development author aliases by default. That flag does not replace the underlying finding. Override the comma-separated aliases with `WP_EXPECTED_DEVELOPMENT_AUTHORS`; an empty value disables the rule.

## Local WordPress REST

Copy `.env.example` to `.env.local` and add a WordPress Application Password. Restart the Flask app. Opening a record in the review sheet refreshes that record from live WordPress and shows the exported and live identity side by side.

To move records to WordPress Trash from the app, also set `WP_REST_WRITE_ENABLED=1`. Trash is local-loopback only, CSRF-protected, confirmed in the browser, capped at 25 records per request, and never uses WordPress `force` delete. In the review sheet, wait for the fresh live check, mark the record Candidate or Approved, and use Move to Trash. The server re-reads the live record and refuses the write if its identity or version changed. Items can be restored from WordPress Trash. Media may refuse trash if `MEDIA_TRASH` is disabled; the app will not permanently delete those files.

Do not add `WP_REST_*` variables to Vercel. Live REST and Trash are local-only.

See [PROJECT_SCOPE.md](PROJECT_SCOPE.md) for definitions, limitations, and the development roadmap.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Vercel deployment procedure and required privacy controls.
