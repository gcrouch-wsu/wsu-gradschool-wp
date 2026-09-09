# Deploying the WordPress Content Audit to Vercel

The application uses one Flask function, Vercel's `public/` directory for browser assets, and a private Vercel Blob store for temporary upload chunks, normalized reports, and review decisions.

Never commit a WordPress export. WXR files can contain unpublished content, deleted-content remnants, author email addresses, and other information that should not be public. The repository `.gitignore` excludes XML exports.

## 1. Import the GitHub repository

1. Sign in at <https://vercel.com/new>.
2. Select **Import Git Repository**.
3. Choose `gcrouch-wsu/wsu-gradschool-wp`.
4. Leave **Root Directory** as `.`.
5. Vercel should detect **Flask**. Do not add a build command or output directory.
6. Select **Deploy**.

The first deployment can display the upload page, but do not upload a real export until private Blob storage and the application secret are configured.

## 2. Create private Blob storage

1. Open the new Vercel project.
2. Go to **Storage** and choose **Create Database** → **Blob**.
3. Create a **Private** store. Private and public stores cannot be converted into each other later.
4. Use the San Francisco region (`sfo1`) to match `vercel.json`.
5. Connect the store to this project for Production, Preview, and Development.
6. Confirm that Vercel added `BLOB_READ_WRITE_TOKEN` to the project's environment variables.

The browser sends the export in 3 MB requests. The function writes those chunks to the private store, assembles and analyzes the export, saves the normalized report, and deletes the raw XML chunks.

## 3. Add the Flask session secret

1. Open **Settings** → **Environment Variables**.
2. Add `FLASK_SECRET_KEY` for Production, Preview, and Development.
3. Use a cryptographically random value of at least 32 bytes. In PowerShell, generate one locally with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Paste only the generated value into Vercel. Do not add it to GitHub.

Optional: add `WP_EXPECTED_DEVELOPMENT_AUTHORS` if the default `greg crouch,gcrouch` aliases need to be changed.

Never add `WP_REST_USERNAME`, `WP_REST_APPLICATION_PASSWORD`, `WP_REST_ENABLED`, or `WP_REST_WRITE_ENABLED` to Vercel. Live WordPress REST and Trash are local-only.

## 4. Protect the application

The export contains non-public content and author metadata. Do not operate this as an unrestricted public website.

1. Open **Settings** → **Deployment Protection**.
2. Enable **Vercel Authentication** with Standard Protection at minimum.
3. On Hobby, Standard Protection does not protect the production domain. Use a protected preview/deployment URL for internal testing.
4. Before using a production domain, enable protection that covers production or add application-level WSU authentication.

## 5. Redeploy and verify

1. Open **Deployments**.
2. Redeploy the latest commit so the Blob environment variable and Flask secret are present.
3. Open the protected deployment URL.
4. Upload a small WordPress export first.
5. Confirm that the dashboard appears and that Pages, Media, taxonomies, details, and exports load.
6. Upload the full Graduate School export and confirm that it produces 6,467 reportable records for the September 9, 2026 file.
7. Change one review decision, refresh, and confirm that the decision remains selected.
8. Select **Analyze a different export** → **Remove this stored audit** when the test is complete.

## 6. Ongoing Git deployment

- Pushes to non-production branches create preview deployments.
- Pushes to `main` update production.
- Validate a protected preview before merging to `main`.
- Review function errors under **Observability** → **Functions**.
- Review and delete abandoned objects in **Storage** → **Blob**. Objects are stored under the `audits/` prefix.

## Local development

Without `BLOB_READ_WRITE_TOKEN`, the application automatically uses process memory:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. Chunked uploads still run locally, but chunks, reports, and decisions disappear when the process stops.

To exercise private Blob storage locally after linking the Vercel project:

```powershell
vercel env pull .env.local
```

Load those values into the shell before starting Flask. Keep `.env.local` out of Git; it is already ignored.
