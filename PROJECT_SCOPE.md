# WSU Graduate School WordPress Content Audit

## Purpose

Build a local, evidence-based review tool that analyzes a WordPress WXR export and helps Graduate School site maintainers identify pages, posts, documents, media, and plugin content that may no longer be needed.

The application supports human decisions and, locally, confirmed WordPress Trash. It does not automatically label anything safe to delete, and it never force-deletes. Its central question is: **What evidence connects this record to the rest of the site, and what evidence is missing?**

For local development, the latest analysis and review decisions live only in process memory. On Vercel, the same workflow uses private Blob-backed audit sessions so requests can move between function instances. Raw XML chunks are removed after analysis, and an audit can be explicitly removed from the interface.

## Current release

The working application now:

- Accepts a WordPress **Tools → Export → All content** WXR XML file.
- Inventories all reportable exported records, including non-public content for context.
- Organizes the inventory into WordPress-oriented groups: Posts, Pages, Events, Media, Managed Documents, Forms, TablePress, Graduate Factsheets, and Other Content.
- Preserves title, WordPress ID, subtype, status, author identity, publication date, last-updated date, URL, parent, and all exported taxonomy assignments.
- Preserves media filename, MIME type, extension, exported file size, dimensions, ALT text, stored path, and generated image variants when present.
- Extracts references from content, excerpts, menus, parent relationships, featured images, attachment relationships, Gutenberg/block markup, shortcodes, and useful custom fields.
- Separates strong, possible, and structural references and retains where-used evidence.
- Identifies expected development content using configurable Greg Crouch author aliases without replacing the underlying finding.
- Provides server-side search, filters, pagination, taxonomy drill-down, record details, in-memory review decisions, a cross-group work queue, and filtered CSV/JSON exports.
- Locally, can check a group against live WordPress REST and move selected records to Trash after confirmation. Trash uses WordPress `DELETE` without `force`, requires a found live check plus candidate/approved review, and is loopback- and CSRF-gated.
- Marks published Events Calendar and graduate factsheet records as needing verification from an assumed public archive; it does not treat that archive as proven reachability.
- Escapes displayed content and protects CSV cells from spreadsheet formula injection.

## Current export results

The September 9, 2026 Graduate School export contains 6,599 raw records and 6,467 reportable inventory records:

| Group | Records | Public | Review findings | Typed taxonomies |
|---|---:|---:|---:|---:|
| Posts | 123 | 108 | 70 | 4 |
| Pages | 306 | 165 | 69 | 5 |
| Events | 271 | 267 | 267 | 1 |
| Media | 3,604 | 3,604 | 2,992 | 4 |
| Managed Documents | 1,020 | 1,002 | 964 | 1 |
| Forms | 669 | 31 | 29 | 1 |
| TablePress | 66 | 66 | 57 | 0 |
| Graduate Factsheets | 213 | 196 | 177 | 2 |
| Other Content | 195 | 192 | 159 | 0 |

These are review counts from that export snapshot, not deletion counts, and they predate archive-assumption and expected-development display changes. Re-analyze the current export to refresh them. A WXR-only finding must be verified before deletion.

## Content organization

Each top-level content group has its own record inventory and summary counts. Records retain their native WordPress or plugin subtype, so a user can distinguish an attachment from a managed `document` record or a normal post from a form-associated post.

Taxonomies are kept as typed relationships rather than flattened into a generic category field. For Pages in this export, the application presents:

- Site Categories (`category`)
- University Categories (`wsuwp_university_category`)
- University Locations (`wsuwp_university_location`)
- University Organizations (`wsuwp_university_org`)
- University Tags (`post_tag`)

Other groups expose the taxonomies that actually apply to them, such as Event Categories or Graduate Degree Type. Each taxonomy shows unique-term, assigned-record, and total-assignment counts; selecting a term filters the matching content records.

## Finding definitions

The tool deliberately uses several explainable conditions instead of one absolute “orphan” flag:

1. **Unreferenced** — no meaningful inbound reference was found in the export.
2. **Unreferenced media** — an attachment is not linked, embedded, used as a featured image, or found in a recognized URL/custom-field reference. A parent/child association alone does not prove a rendered use.
3. **Disconnected island** — references exist, but neither the record nor its sources are reachable from recognized site entry points.
4. **Needs verification** — evidence is incomplete, indirect, or held in a structure that requires human/live-site confirmation.
5. **Expected development** — an independent identity flag for configured development-author aliases, currently Greg Crouch / `gcrouch`. The primary finding remains the underlying classification.
6. **Non-public** — the record is draft, private, pending, trashed, or otherwise not public.
7. **Linked** — useful inbound evidence was found from a menu, the home URL, or linked reachable content. Assumed public archives are not sufficient for this finding.

Confidence and reasons accompany each finding. Authorship alone is never evidence that deletion is safe.

## Review workflow

The intended workflow is:

1. Select a content group.
2. Filter by finding, status, author, subtype, taxonomy term, or search text.
3. Open record details and inspect metadata plus where-used evidence.
4. Assign an in-memory review decision: Keep, Expected Development, Verify, Deletion Candidate, or Approved to Delete.
5. Export the current filtered view to CSV or JSON, or use the work queue to review unreferenced records, live-missing records, and deletion candidates.

Locally, with `WP_REST_WRITE_ENABLED=1`, the app can move selected records to WordPress Trash after an explicit confirmation. It never force-deletes. Restore remains a WordPress admin action.

## What a WXR export can and cannot prove

A full WXR export is a strong source for WordPress IDs, content, authors, dates, statuses, parent relationships, attachment URLs, selected metadata, menus, and taxonomy assignments.

It does not reliably contain theme templates, widgets, Customizer/site options, plugin database tables, all reusable/global content, CSS background images, JavaScript-generated links, external-site links, analytics usage, or the final live-rendered page graph. Gravity Forms definitions and field settings are not included in this export; only form-associated post records and IDs are available. The Forms group is therefore explicitly marked as partial data.

Consequently, “unreferenced in export” means exactly that. It is not equivalent to “unused everywhere.”

## Safety and privacy requirements

- Stay local-only for WordPress REST credentials and writes. Never put `WP_REST_*` on Vercel.
- Trash is opt-in (`WP_REST_WRITE_ENABLED`), confirmed in the UI, and never uses WordPress `force` delete.
- Keep uploaded data and review decisions local and in memory for the current local-dev phase. On Vercel, reports use private Blob storage; abandoned upload chunks expire after one hour when a later upload refreshes the chunk index.
- Parse XML with protections appropriate for untrusted uploads.
- Preserve original evidence while normalizing URLs for matching.
- Escape uploaded values in the interface.
- Protect spreadsheet exports against formula injection.
- Make uncertainty and missing source coverage visible.

## Architecture

```text
wsu-gradschool-wp/
  app.py                    Flask UI and JSON/export endpoints
  analyzer/
    models.py               normalized in-memory records
    wxr_parser.py           protected WXR parsing
    url_normalizer.py       URL matching variants
    analysis.py             extraction, graph, classification, grouping
    wp_rest.py              local live-check and Trash client
  local_env.py              local .env.local loader; Vercel never loads it
  templates/index.html      dashboard and upload workflow
  public/app.js             interactive filtering and chunked upload workflow
  public/styles.css         responsive WSU-oriented visual system
  audit_store.py            memory/private-Blob storage adapter
  vercel.json               Vercel function configuration
  tests/                    parser, classifier, API, and export tests
  PROJECT_SCOPE.md
```

The analyzer is independent of Flask so it can be tested directly and later reused by a command-line task or another interface.

## Next development phases

### Phase 2 — Accuracy validation

- Compare representative known-active, intentionally hidden, obsolete, and Greg Crouch development records against the findings.
- Expand recognized plugin metadata and serialized relationship extraction where the export contains usable evidence.
- Add explicit inclusion/exclusion rules for trusted entry points and organizational policies.
- Improve duplicate/revision detection for attachments and managed documents.

### Phase 3 — Optional live confirmation

Local REST live-check and confirmed Trash are already implemented. Remaining work:

- Add an explicitly enabled, read-only crawl of the public site.
- Compare live status codes, redirects, canonical URLs, links, embeds, and asset use with export findings.
- Keep sitemap presence separate from genuine reachability.
- Never submit forms from a crawler. REST writes stay local, opt-in, Trash-only, and never use `force`.

### Phase 4 — Persistence and governance

- Persist analysis runs and review decisions in a local database.
- Record reviewer, timestamp, notes, and decision history.
- Support comparison between exports to show additions, removals, and changed evidence.
- Produce an approved action list that can be executed through the existing local Trash path or a separate controlled WordPress cleanup process.

### Phase 5 — REST-first inventory, export as deep audit

A WXR export remains required for the current orphan/where-used graph. REST is a better source for “what is live right now.” Do not treat REST as a drop-in replacement for the export.

**Intended shape:** hybrid, not REST-only.

1. Open the dashboard from authenticated local REST and populate inventory, live status, and Trash without an upload.
2. Keep the export, or an equivalent authenticated content crawl, as the optional source for the reference graph: where-used evidence, unreferenced media, and disconnected islands.
3. Weaken or disable export-only findings when no snapshot is loaded, rather than guessing from titles and IDs.
4. Use REST for smaller groups first (posts, pages, events). Media (~3,600), documents, and forms can stay export-backed until pagination and coverage are proven.
5. Mark groups that REST cannot cover (TablePress, Gravity Forms definitions, some Other plugin types) as partial or unavailable.
6. Keep all WordPress credentials and writes local. Never put `WP_REST_*` on Vercel.

**Write preflight required before expanding REST-first inventory**

- Bind the export or crawl to a verified site identity (scheme, host, port, and site path) matching `WP_REST_BASE_URL`.
- Re-fetch the current ID, type, title, and status from that site immediately before Trash; refuse on identity drift.
- Discover each endpoint’s schema, page size, and pagination; incomplete or off-contract list responses are errors, not “missing.”
- Represent REST vs WXR conflicts and completeness per field/group instead of silently preferring one source.
- Use least-privilege, revocable Application Passwords; require HTTPS except loopback; refuse cross-origin redirects.
- Do not enable REST-first writes until these are measurable per group: snapshot watermark (checked_at + source), cross-endpoint consistency, capability discovery (route, status enum, include/pagination contract), conflict-resolution (revisioned live/decision writes), resumability, and an explicit complete / partial / unavailable state.

**Constraints this phase must respect**

- A live catalog answers existence and status. It does not prove unused-everywhere.
- Plugin REST quirks are expected (for example, The Events Calendar replacing the status enum so `POST status=trash` fails; Trash must stay `DELETE` without `force`).
- Menus, rendered/block HTML, shortcodes, and useful meta are required for trustworthy orphan findings. Listing endpoints are not enough.
- A full REST content crawl that downloads the same fields as WXR is acceptable; skipping the snapshot is not.
- An “equivalent authenticated content crawl” cannot assume unregistered meta, menus, plugin tables, or rendered shortcode relationships are available.

## Success criteria

The current release is useful when it can reliably inventory the export, preserve rich metadata, expose typed taxonomies, explain why each finding was made, distinguish expected development content without suppressing it, and produce a filtered review artifact. It must always communicate that final deletion decisions require human confirmation and, for uncertain cases, a live-site or plugin-specific check.
