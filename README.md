# WSU Graduate School WordPress Orphan Analyzer

Flask application for analyzing a WordPress WXR export. It inventories content and media, extracts references, builds a link graph, and presents explainable orphan candidates for human review. It runs locally in memory or on Vercel with private Blob-backed audit sessions.

## Run locally

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>, select **Tools → Export → All content** from WordPress, and upload the resulting XML file.

In local mode, the latest report and review decisions remain in memory until the process stops or another export is analyzed. On Vercel, private Blob storage keeps the report available across function invocations; temporary raw XML chunks are deleted after analysis. The application does not modify WordPress or delete content.

The dashboard groups records into Posts, Pages, Events, Media, Documents, Forms, TablePress, Factsheets, and Other. Filters, taxonomy drill-downs, evidence details, review decisions, and CSV/JSON exports all operate on the current in-memory report.

Greg Crouch and `gcrouch` are treated as expected-development author aliases by default. Override the comma-separated aliases with the `WP_EXPECTED_DEVELOPMENT_AUTHORS` environment variable.

See [PROJECT_SCOPE.md](PROJECT_SCOPE.md) for definitions, limitations, and the development roadmap.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Vercel deployment procedure and required privacy controls.
