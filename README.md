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

Live WordPress states (found, in Trash, missing) belong to the analyzed export, not to the site: analyzing a replacement export resets every record to **Not checked**, and the "Live WordPress" filter — including **In trash** — stays empty until records are checked. Use **Check live WordPress** in the group header to check every record in the selected group (or the work queue) in batches of 100; the coverage strip under the summary shows how many records have been checked and how many are in Trash or missing. Opening a record in the review sheet still checks that single record.

During upload the overlay shows the upload step with bytes sent, the single analysis step with an elapsed clock, and then the dashboard opening. If the analysis response is lost (for example a gateway timeout on a very large export), the page polls `/api/upload-status` and adopts the finished report instead of re-analyzing; completing the same upload twice never re-analyzes.

In local mode, the latest report and review decisions remain in memory until the process stops or another export is analyzed. On Vercel, private Blob storage keeps each signed-in reviewer's current audit available across function invocations. Reports are owner-bound and expire after 48 hours when next accessed. Temporary raw XML chunks are deleted after analysis, and abandoned Blob chunks expire after one hour when a later upload refreshes the chunk index.

The dashboard groups records into Posts, Pages, Events, Media, Documents, Forms, TablePress, Factsheets, and Other. Filters, taxonomy drill-downs, evidence details, review decisions, and CSV/JSON exports operate on the stored report for the current session. Published Events and graduate factsheets are marked needs-verification from an assumed public archive; the export cannot prove those archives are enabled or complete. Exported menus are used as entry points only when their status is publish, and even then the export cannot prove a live theme location.

Greg Crouch and `gcrouch` are treated as expected-development author aliases by default. That flag does not replace the underlying finding. Override the comma-separated aliases with `WP_EXPECTED_DEVELOPMENT_AUTHORS`; an empty value disables the rule.

## WordPress REST and Trash

Use a dedicated, least-privilege WordPress account and a revocable Application Password. Opening a record in the review sheet refreshes that record from live WordPress and shows the exported and live identity side by side.

### Creating the WordPress Application Password

Application Passwords are built into WordPress 5.6 and later and are separate from the account's sign-in password. They only work over HTTPS and can be revoked individually without changing the account password.

1. Decide which WordPress account will act for this app. Live checks need an account that can edit the content types being audited (the app reads with `context=edit` so that draft, pending, private, and trashed records are returned); moving other people's records to Trash needs `delete_others_posts`. An **Editor** role covers posts, pages, and the WSU document type. Do not use a personal WSU Network ID account if a dedicated account is available.
2. Sign in to WordPress as that account (or as an administrator editing that account) and open **Users → Profile** — for another account, **Users → All Users → the account → Edit**.
3. Scroll to the **Application Passwords** section near the bottom of the profile. If the section is missing, the site is not served over HTTPS or a plugin or the WSU network configuration has disabled Application Passwords; ask WSU Web Services to enable them for the account.
4. In **New Application Password Name**, enter a label that identifies this tool, for example `Graduate School content audit`, and choose **Add New Application Password**.
5. WordPress shows the 24-character password **once**. Copy it immediately; the spaces WordPress displays are optional and may be kept or removed. If it is lost, revoke it and create a new one.
6. Store the values only where the app reads them: `WP_REST_USERNAME` is the WordPress **username** (not the display name or email) and `WP_REST_APPLICATION_PASSWORD` is the copied password. Locally that is `.env.local` (never committed); on Vercel they are Production environment variables (see [DEPLOYMENT.md](DEPLOYMENT.md)). The local archive tool accepts the same username and Application Password in its credentials fields and keeps them in memory only.
7. Confirm the credential works with a read-only check first: open any record in the review sheet, or use **Check live WordPress** on a small group, before enabling `WP_REST_WRITE_ENABLED`.
8. To revoke, return to the same **Application Passwords** section and choose **Revoke** next to the entry (or **Revoke all application passwords**). Revoke immediately if the Vercel project, `.env.local`, or reviewer access is ever compromised, and rotate the password when a reviewer leaves.

The password authenticates REST requests only; it cannot be used to sign in to wp-admin. This app sends it as HTTP Basic authentication to `WP_REST_BASE_URL` over HTTPS and never to any other host.

To move records to WordPress Trash, set `WP_REST_WRITE_ENABLED=1`. Vercel additionally requires `WP_REST_REMOTE_WRITE_ENABLED=1` and `VERCEL_ENV=production`; Preview deployments remain read-only. For one record, open its review card, wait for the live check, choose **Approve to delete**, then choose **Move to Trash**. For a bulk action, select up to 25 REST-supported rows without approving each one first, choose **Review selected for Trash**, inspect the fresh WordPress preflight results, and confirm the ready records. Trash requires an authenticated session and CSRF token and never uses WordPress `force` delete. The server re-reads every live record and refuses the write if its identity or version changed. An uncertain DELETE response is never retried; the app reads the record back before deciding whether the move completed.

Store WordPress credentials only as server-side Vercel environment variables. Do not expose them to browser code, reports, exports, logs, Preview deployments, or Git. Each Trash result records the signed-in reviewer and limited action metadata (site, WordPress ID/type, export status, time, and result) in a private immutable Blob object; it does not copy the record title or body. Items can be restored from WordPress Trash until the site's Trash retention removes them. Media may refuse Trash if `MEDIA_TRASH` is disabled; the app will not permanently delete those files.

See [PROJECT_SCOPE.md](PROJECT_SCOPE.md) for definitions, limitations, and the development roadmap.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Vercel deployment procedure and required privacy controls.
