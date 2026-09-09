import csv
import io
import json
import xml.etree.ElementTree as ET
from flask import Flask, render_template_string, request, Response

app = Flask(__name__)
# Allow up to 250MB XML file uploads
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>WordPress Orphan Content Finder</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --tag-post: #f59e0b;
      --tag-page: #10b981;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 2rem 1rem; line-height: 1.5; }
    .container { max-width: 1000px; margin: 0 auto; }
    header { margin-bottom: 2rem; }
    h1 { font-size: 1.75rem; margin-bottom: 0.5rem; color: #fff; }
    p.subtitle { color: var(--muted); font-size: 0.95rem; }

    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
    .upload-box { border: 2px dashed var(--border); border-radius: 6px; padding: 2.5rem 1.5rem; text-align: center; cursor: pointer; transition: border-color 0.2s; }
    .upload-box:hover { border-color: var(--accent); }
    input[type="file"] { display: none; }

    .btn { background: var(--accent); color: #000; border: none; padding: 0.6rem 1.2rem; font-weight: 600; border-radius: 4px; cursor: pointer; display: inline-block; text-decoration: none; }
    .btn:hover { background: var(--accent-hover); color: #fff; }
    .btn-secondary { background: #334155; color: #fff; }
    .btn-secondary:hover { background: #475569; }

    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .stat-card { background: #0b1120; border: 1px solid var(--border); border-radius: 6px; padding: 1rem; text-align: center; }
    .stat-val { font-size: 1.75rem; font-weight: 700; color: var(--accent); }
    .stat-label { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }

    .header-actions { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }
    th { background: #0b1120; padding: 0.75rem; color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border); }
    td { padding: 0.75rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
    tr:hover { background: rgba(255, 255, 255, 0.02); }

    .badge { padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
    .badge-page { background: rgba(16, 185, 129, 0.2); color: var(--tag-page); }
    .badge-post { background: rgba(245, 158, 11, 0.2); color: var(--tag-post); }
    .item-url { color: var(--muted); font-size: 0.8rem; word-break: break-all; text-decoration: none; }
    .item-url:hover { color: var(--accent); }
    .error { color: #ef4444; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); padding: 1rem; border-radius: 6px; margin-bottom: 1rem; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>WordPress Orphan Content Scanner</h1>
      <p class="subtitle">Find published pages and posts that are not linked to any WordPress navigation menu.</p>
    </header>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    <div class="card">
      <form id="upload-form" method="POST" enctype="multipart/form-data">
        <label class="upload-box" for="file-input">
          <div id="upload-text">
            <strong>Choose or drag your WordPress XML export file here</strong>
            <p style="color: var(--muted); font-size: 0.85rem; margin-top: 0.4rem;">Supports standard Tools &gt; Export WXR (.xml) files</p>
          </div>
          <input id="file-input" type="file" name="export_file" accept=".xml" onchange="document.getElementById('upload-form').submit()">
        </label>
      </form>
    </div>

    {% if results %}
    <div class="stats">
      <div class="stat-card">
        <div class="stat-val">{{ stats.total_published }}</div>
        <div class="stat-label">Total Published</div>
      </div>
      <div class="stat-card">
        <div class="stat-val">{{ stats.menu_targets }}</div>
        <div class="stat-label">Menu Items Found</div>
      </div>
      <div class="stat-card">
        <div class="stat-val" style="color: #ef4444;">{{ stats.unlinked_count }}</div>
        <div class="stat-label">Unlinked (Orphans)</div>
      </div>
    </div>

    <div class="card">
      <div class="header-actions">
        <h2>Unlinked Content ({{ results|length }})</h2>
        <button class="btn" onclick="downloadCSV()">Download as CSV</button>
      </div>

      <div style="overflow-x: auto; max-height: 600px; overflow-y: auto;">
        <table>
          <thead>
            <tr>
              <th style="width: 70px;">ID</th>
              <th style="width: 80px;">Type</th>
              <th>Title</th>
              <th>URL</th>
            </tr>
          </thead>
          <tbody>
            {% for item in results %}
            <tr>
              <td>{{ item.id }}</td>
              <td><span class="badge badge-{{ item.type }}">{{ item.type }}</span></td>
              <td><strong>{{ item.title }}</strong></td>
              <td><a class="item-url" href="{{ item.url }}" target="_blank">{{ item.url }}</a></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <script>
      const reportData = {{ results|tojson }};
      function downloadCSV() {
        let csvContent = "data:text/csv;charset=utf-8,ID,Type,Title,URL\\n";
        reportData.forEach(row => {
          const cleanTitle = `"${(row.title || '').replace(/"/g, '""')}"`;
          const cleanUrl = `"${(row.url || '').replace(/"/g, '""')}"`;
          csvContent += `${row.id},${row.type},${cleanTitle},${cleanUrl}\\n`;
        });
        const encodedUri = encodeURI(csvContent);
        const link = document.createElement("a");
        link.setAttribute("href", encodedUri);
        link.setAttribute("download", "unlinked_wordpress_content.csv");
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }
    </script>
    {% endif %}
  </div>
</body>
</html>
"""

def parse_wordpress_xml(file_stream):
    tree = ET.parse(file_stream)
    root = tree.getroot()
    channel = root.find('channel')
    if channel is None:
        raise ValueError("Invalid WordPress export: missing <channel> root.")

    published_items = {}
    menu_object_ids = set()
    menu_target_urls = set()

    for item in channel.findall('item'):
        # Match elements with or without namespaces
        post_type_el = item.find('{*}post_type')
        post_id_el = item.find('{*}post_id')
        status_el = item.find('{*}status')
        title_el = item.find('title')
        link_el = item.find('link')

        post_type = post_type_el.text if post_type_el is not None else None
        post_id = post_id_el.text if post_id_el is not None else None
        status = status_el.text if status_el is not None else ''
        title = title_el.text if title_el is not None and title_el.text else 'Untitled'
        url = link_el.text if link_el is not None and link_el.text else ''

        if not post_type:
            continue

        # 1. Track published pages and posts
        if post_type in ['post', 'page'] and status == 'publish':
            published_items[post_id] = {
                'id': post_id,
                'type': post_type,
                'title': title,
                'url': url
            }

        # 2. Extract menu target links and object IDs
        elif post_type == 'nav_menu_item':
            for meta in item.findall('{*}postmeta'):
                key = meta.find('{*}meta_key')
                val = meta.find('{*}meta_value')
                if key is not None and val is not None and val.text:
                    if key.text == '_menu_item_object_id' and val.text != '0':
                        menu_object_ids.add(val.text)
                    elif key.text == '_menu_item_url':
                        menu_target_urls.add(val.text.rstrip('/'))

    # 3. Filter down to unlinked items
    unlinked = []
    for pid, data in published_items.items():
        clean_url = data['url'].rstrip('/') if data['url'] else ''
        if pid not in menu_object_ids and clean_url not in menu_target_urls:
            unlinked.append(data)

    stats = {
        'total_published': len(published_items),
        'menu_targets': len(menu_object_ids) + len(menu_target_urls),
        'unlinked_count': len(unlinked)
    }

    return unlinked, stats

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'export_file' not in request.files:
            return render_template_string(HTML_TEMPLATE, error="No file uploaded.")

        file = request.files['export_file']
        if file.filename == '':
            return render_template_string(HTML_TEMPLATE, error="No file selected.")

        try:
            unlinked, stats = parse_wordpress_xml(file.stream)
            return render_template_string(HTML_TEMPLATE, results=unlinked, stats=stats)
        except Exception as e:
            return render_template_string(HTML_TEMPLATE, error=f"Failed to parse XML: {str(e)}")

    return render_template_string(HTML_TEMPLATE)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
