# Deploying the WordPress Content Audit to Vercel

The application uses one Flask function, Vercel's `public/` directory for browser assets, and a private Vercel Blob store for temporary upload chunks, normalized reports, review decisions, live-check state, and Trash action records.

Never commit a WordPress export. WXR files can contain unpublished content, deleted-content remnants, author email addresses, and other information that should not be public. The repository `.gitignore` excludes XML exports.

## 1. Import the GitHub repository

1. Sign in at <https://vercel.com/new>.
2. Select **Import Git Repository**.
3. Choose `gcrouch-wsu/wsu-gradschool-wp`.
4. Leave **Root Directory** as `.`.
5. Vercel should detect **Flask**. Do not add a build command or output directory.
6. Select **Deploy**.

The deployment fails closed at the sign-in page until private Blob storage, the application secret, and the reviewer allowlist are configured.

## 2. Create private Blob storage

1. Open the new Vercel project.
2. Go to **Storage** and choose **Create Database** → **Blob**.
3. Create a **Private** store. Private and public stores cannot be converted into each other later.
4. Use the San Francisco region (`sfo1`) to match `vercel.json`.
5. Connect the store to this project for Production, Preview, and Development.
6. Confirm that Vercel added `BLOB_READ_WRITE_TOKEN` to the project's environment variables.

The browser sends the export in 3 MB requests. The function writes those chunks to the private store, assembles and analyzes the export, saves the normalized report, and deletes the raw XML chunks.

## 3. Configure application sign-in

1. Open **Settings** → **Environment Variables**.
2. Add `FLASK_SECRET_KEY` for Production, Preview, and Development.
3. Use a cryptographically random value of at least 32 bytes. In PowerShell, generate one locally with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Paste only the generated value into Vercel. Do not add it to GitHub.
5. For each reviewer, choose an app-specific password locally. Do not use a WSU Network ID password. Generate its hash locally:

   ```powershell
   python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('App password: ')))"
   ```

6. Build one JSON object mapping each exact WSU email address to its hash, for example `{"reviewer@wsu.edu":"scrypt:..."}`. Add that object as `APP_AUTH_USERS_JSON` for Production, Preview, and Development. Never store the passwords or hashes in Git.

Optional: add `WP_EXPECTED_DEVELOPMENT_AUTHORS` if the default `greg crouch,gcrouch` aliases need to be changed.

## 4. Configure WordPress REST and Trash

Create a dedicated, least-privilege WordPress account and Application Password (WordPress **Users → Profile → Application Passwords → Add New Application Password**; the password is shown once — see "Creating the WordPress Application Password" in [README.md](README.md) for the full steps and the required role). Store these values only as Vercel server-side environment variables; they must never appear in browser code, Blob reports, exports, logs, or Git.

Add these variables to **Production only**:

- `WP_REST_BASE_URL=https://gradschool.wsu.edu`
- `WP_REST_USERNAME=<dedicated WordPress username>`
- `WP_REST_APPLICATION_PASSWORD=<revocable Application Password>`
- `WP_REST_ENABLED=1`
- `WP_REST_WRITE_ENABLED=1`
- `WP_REST_REMOTE_WRITE_ENABLED=1`

`WP_REST_REMOTE_WRITE_ENABLED` is the additional production-only safety gate. The code refuses Vercel writes outside `VERCEL_ENV=production`, so Preview remains read-only even if variables are accidentally copied there. WordPress REST must use HTTPS, the uploaded export must match `WP_REST_BASE_URL`, and the browser request must be authenticated, CSRF-protected, and same-origin.

First deploy with `WP_REST_WRITE_ENABLED=0`, verify live checks against a disposable record (open it in the review sheet, or use **Check live WordPress** on a small group and confirm the coverage strip shows checked records), then enable both write flags and verify one reversible Trash operation. Revoke the Application Password immediately if the Vercel project or reviewer access is compromised.

## 5. Protect the application

The export contains non-public content and author metadata. Do not operate this as an unrestricted public website.

1. Open **Settings** → **Deployment Protection**.
2. Keep the application allowlist limited to the three named reviewers and remove access promptly when it is no longer needed.
3. Add a Vercel Firewall rate-limit rule for `POST /login`. The built-in application throttle is session-local and is not a substitute for an IP/platform limit.
4. Optionally enable Vercel Authentication as a second layer where all reviewers can be granted Vercel project access.

## 6. Redeploy and verify

1. Open **Deployments**.
2. Redeploy the latest commit so the Blob environment variable and Flask secret are present.
3. Confirm an unrecognized email and a wrong password both fail with the same message.
4. Sign in and upload a small, fresh WordPress export first.
5. Open a disposable record, wait for the live check, choose **Approve to delete**, and move it to Trash from the same review card.
6. Restore the disposable record in WordPress, then select two unapproved disposable records with the table checkboxes, choose **Review selected for Trash**, inspect the fresh preflight summary, and move the ready records to Trash.
7. Confirm Preview deployments show REST as read-only and cannot move records to Trash.
8. Upload the full Graduate School export and confirm that Pages, Media, taxonomies, details, and exports load. Do not use a historical record count as a deployment health check.
9. Change one review decision, refresh, and confirm that the decision remains selected.
10. Sign out and confirm the audit cannot be opened from another configured reviewer's session.
11. Select **Analyze a different export** → **Remove this stored audit** when the test is complete.

## 7. Ongoing Git deployment

- Pushes to non-production branches create preview deployments.
- Pushes to `main` update production.
- Validate a protected preview before merging to `main`.
- Review function errors under **Observability** → **Functions**.
- Review Blob retention regularly. Audit objects are stored under `audits/`; successful Trash attempts are recorded separately under `audit-actions/`.
- An audit expires after 48 hours when it is next accessed. Abandoned chunks expire after one hour when a later upload runs cleanup. Configure an external retention check if policy requires deletion at an exact deadline even when nobody returns to the app.

## Local development

Without `BLOB_READ_WRITE_TOKEN`, the application automatically uses process memory:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. Chunked uploads still run locally, but chunks, reports, and decisions disappear when the process stops.

Set `APP_AUTH_USERS_JSON` and `FLASK_SECRET_KEY` in `.env.local`. To enable local live checks and Trash, add the WordPress variables above except `WP_REST_REMOTE_WRITE_ENABLED`; local writes remain restricted to loopback requests.

To exercise private Blob storage locally after linking the Vercel project:

```powershell
vercel env pull .env.local
```

Load those values into the shell before starting Flask. Keep `.env.local` out of Git; it is already ignored.
