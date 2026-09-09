# WSU Graduate School WordPress Content Audit

## Purpose

Build a local, evidence-based review tool that analyzes a WordPress WXR export and helps Graduate School site maintainers identify pages, posts, documents, media, and plugin content that may no longer be needed.

The application supports human decisions; it does not modify WordPress or automatically label anything safe to delete. Its central question is: **What evidence connects this record to the rest of the site, and what evidence is missing?**

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
- Identifies expected development content using configurable Greg Crouch author aliases without hiding that content.
- Provides server-side search, filters, pagination, taxonomy drill-down, record details, in-memory review decisions, and filtered CSV/JSON exports.
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

These are review counts, not deletion counts. A WXR-only finding must be verified before deletion.

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
2. **Unreferenced media** — an attachment is not linked, embedded, used as a featured image, associated with a parent, or found in recognized metadata.
3. **Disconnected island** — references exist, but neither the record nor its sources are reachable from recognized site entry points.
4. **Needs verification** — evidence is incomplete, indirect, or held in a structure that requires human/live-site confirmation.
5. **Expected development** — the record matches an explicit development-author rule, currently Greg Crouch / `gcrouch`; its underlying finding remains available.
6. **Non-public** — the record is draft, private, pending, trashed, or otherwise not public.
7. **Linked** — useful inbound evidence was found.

Confidence and reasons accompany each finding. Authorship alone is never evidence that deletion is safe.

## Review workflow

The intended workflow is:

1. Select a content group.
2. Filter by finding, status, author, subtype, taxonomy term, or search text.
3. Open record details and inspect metadata plus where-used evidence.
4. Assign an in-memory review decision: Keep, Expected Development, Verify, Deletion Candidate, or Approved to Delete.
5. Export the current filtered view to CSV or JSON for review or action outside this application.

The application never performs the deletion. A future persistent release should also require an explicit verification checkpoint before a record can receive an approved-to-delete decision.

## What a WXR export can and cannot prove

A full WXR export is a strong source for WordPress IDs, content, authors, dates, statuses, parent relationships, attachment URLs, selected metadata, menus, and taxonomy assignments.

It does not reliably contain theme templates, widgets, Customizer/site options, plugin database tables, all reusable/global content, CSS background images, JavaScript-generated links, external-site links, analytics usage, or the final live-rendered page graph. Gravity Forms definitions and field settings are not included in this export; only form-associated post records and IDs are available. The Forms group is therefore explicitly marked as partial data.

Consequently, “unreferenced in export” means exactly that. It is not equivalent to “unused everywhere.”

## Safety and privacy requirements

- Remain read-only with respect to WordPress.
- Never delete or mutate site content.
- Keep uploaded data and review decisions local and in memory for the current phase.
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

- Add an explicitly enabled, read-only crawl of the public site.
- Compare live status codes, redirects, canonical URLs, links, embeds, and asset use with export findings.
- Keep sitemap presence separate from genuine reachability.
- Never submit forms, authenticate to WordPress, or mutate the site.

### Phase 4 — Persistence and governance

- Persist analysis runs and review decisions in a local database.
- Record reviewer, timestamp, notes, and decision history.
- Support comparison between exports to show additions, removals, and changed evidence.
- Produce an approved action list for a separate, controlled WordPress cleanup process.

## Success criteria

The current release is useful when it can reliably inventory the export, preserve rich metadata, expose typed taxonomies, explain why each finding was made, distinguish expected development content without suppressing it, and produce a filtered review artifact. It must always communicate that final deletion decisions require human confirmation and, for uncertain cases, a live-site or plugin-specific check.
