import time
import subprocess
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, abort

from restic import ResticUI

app = Flask(__name__)

@app.route("/")
def default_route():
    restic = ResticUI()
    # Cache snapshots in memory for 10 seconds to reduce CLI calls
    if not hasattr(restic, "_cached_snapshots") or (restic._cache_time + 10) < time.time():
        restic._cached_snapshots = restic.get_snapshots()
        restic._cache_time = time.time()
    snapshots = restic._cached_snapshots
    template = """
    <h1>Restic Snapshots</h1>
    {% if snapshots %}
    <ul>
    {% for snap in snapshots %}
      <li><a href="/snapshot/{{ snap.id }}">{{ snap.id }}</a> - {{ snap.time }} - {{ snap.hostname }}</li>
    {% endfor %}
    </ul>
    {% else %}
    <p>No snapshots found.</p>
    {% endif %}
    """
    return render_template_string(template, snapshots=snapshots)

@app.route("/snapshot/<snapshot_id>", methods=["GET", "POST"])
def snapshot_detail(snapshot_id):
    restic = ResticUI()

    if request.method == "POST":
        selected_paths = request.form.getlist("selected_paths")
        restore_path = request.form.get("restore_path", "").strip()
        if selected_paths and restore_path:
            for path in selected_paths:
                subprocess.run([
                    "restic", "restore", snapshot_id,
                    "--target", restore_path,
                    "--include", path
                ])
            return redirect(url_for("snapshot_detail", snapshot_id=snapshot_id))

    # Initial page render: we don't build the whole tree here anymore
    template = """
    <h1>Snapshot {{ snapshot_id }} Contents</h1>
    <style>
      .caret {
        cursor: pointer;
        user-select: none;
        display: inline-block;
        margin-left: 6px;
        color: black;
      }
      .caret::before {
        content: "\\25B6"; /* right-pointing triangle */
        display: inline-block;
        transform: rotate(0deg);
        transition: transform 0.2s ease;
      }
      .caret-down::before {
        transform: rotate(90deg);
      }
      .nested {
        margin-left: 1em;
      }
      form {
        margin-top: 1em;
      }
      label {
        cursor: pointer;
      }
      .loading {
        font-style: italic;
        color: #666;
      }
      .error {
        color: red;
      }
    </style>

    <form id="restore-form" method="post" onsubmit="return confirmRestore()">
      <div id="tree-container" class="loading">
        Loading snapshot contents...
      </div>
      <p>
        <label for="restore_path">Restore Path:</label>
        <input type="text" id="restore_path" name="restore_path" required placeholder="/path/to/restore">
      </p>
      <button type="submit">Restore Selected</button>
    </form>

    <p><a href="/">Back to snapshots</a></p>

    <script>
      async function loadRoot() {
        const container = document.getElementById('tree-container');
        container.classList.add('loading');
        container.textContent = 'Loading snapshot contents...';
        try {
          const response = await fetch('{{ url_for("snapshot_tree_root_api", snapshot_id=snapshot_id) }}');
          if (!response.ok) {
            container.classList.remove('loading');
            container.classList.add('error');
            container.textContent = 'Error loading snapshot contents.';
            return;
          }
          const data = await response.json();
          container.classList.remove('loading');
          container.innerHTML = buildNodeListHtml(data.entries, '');
        } catch (e) {
          container.classList.remove('loading');
          container.classList.add('error');
          container.textContent = 'Error loading snapshot contents.';
        }
      }

      function escapeHtml(text) {
        const map = {
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#039;'
        };
        return text.replace(/[&<>"']/g, function(m) { return map[m]; });
      }

      function buildNodeListHtml(entries, prefix) {
        // entries: [{name, path, type}]
        let html = '<ul style="list-style-type:none; padding-left: 1em;">';
        for (const entry of entries) {
          const fullPath = entry.path;
          const safeId = escapeHtml(fullPath).replace(/[^a-zA-Z0-9_-]/g, '_');
          const safeLabel = escapeHtml(entry.name);
          const checkbox = '<input type="checkbox" name="selected_paths" value="' + escapeHtml(fullPath) + '" id="' + safeId + '">';
          const label = '<label for="' + safeId + '">' + safeLabel + '</label>';

          if (entry.type === 'dir') {
            // Directory: has caret and a nested container that will be filled lazily
            html += '<li>'
              + checkbox + label + ' '
              + '<span class="caret" data-path="' + escapeHtml(fullPath) + '" data-loaded="false" onclick="onCaretClick(this)"></span>'
              + '<div class="nested" style="display:none;"></div>'
              + '</li>';
          } else {
            // File: just checkbox + label
            html += '<li>' + checkbox + label + '</li>';
          }
        }
        html += '</ul>';
        return html;
      }

      async function onCaretClick(element) {
        const nested = element.nextElementSibling;
        const path = element.getAttribute('data-path');
        const loaded = element.getAttribute('data-loaded') === 'true';

        // Toggle visibility
        if (nested.style.display === 'none') {
          nested.style.display = 'block';
          element.classList.add('caret-down');
        } else if (loaded) {
          // If already loaded, just hide/show
          nested.style.display = 'none';
          element.classList.remove('caret-down');
          return;
        }

        if (loaded) {
          // Already loaded and we just showed it above
          return;
        }

        // Not loaded yet: fetch children
        nested.innerHTML = '<span class="loading">Loading...</span>';
        try {
          const url = new URL('{{ url_for("snapshot_tree_node_api", snapshot_id=snapshot_id) }}', window.location.origin);
          url.searchParams.set('path', path);
          const response = await fetch(url.toString());
          if (!response.ok) {
            nested.innerHTML = '<span class="error">Error loading directory.</span>';
            return;
          }
          const data = await response.json();
          nested.innerHTML = buildNodeListHtml(data.entries, path);
          element.setAttribute('data-loaded', 'true');
        } catch (e) {
          nested.innerHTML = '<span class="error">Error loading directory.</span>';
        }
      }

      function confirmRestore() {
        const checked = document.querySelectorAll('input[name="selected_paths"]:checked');
        if (checked.length === 0) {
          alert("Please select at least one path to restore.");
          return false;
        }
        const path = document.getElementById("restore_path").value.trim();
        if (!path) {
          alert("Please enter a restore path.");
          return false;
        }
        return confirm(`Restore ${checked.length} item(s) to "${path}"?`);
      }

      document.addEventListener('DOMContentLoaded', loadRoot);
    </script>
    """
    return render_template_string(template, snapshot_id=snapshot_id)

def _list_children(entries, parent_path):
    """
    Given a list of restic entries (dicts with 'path' and 'type') and a parent
    path ('' for root), return a list of direct children entries:
      [{ "name": <str>, "path": <str>, "type": "file"|"dir" }]
    """
    children = {}
    # Normalise parent path: '' (root) or 'dir/subdir'
    prefix = parent_path.strip('/')
    if prefix:
        prefix = prefix + '/'
    else:
        prefix = ''

    for e in entries:
        path = e.get("path", "").lstrip("/")
        if not path.startswith(prefix):
            continue

        rest = path[len(prefix):]
        if not rest:
            continue

        # Only direct children: split once
        parts = rest.split('/', 1)
        name = parts[0]
        is_dir = len(parts) > 1 or e.get("type") == "dir"
        child_path = (prefix + name).rstrip('/')

        existing = children.get(name)
        if existing:
            # Upgrade to dir if any entry indicates it's a dir
            if is_dir and existing["type"] == "file":
                existing["type"] = "dir"
            continue

        children[name] = {
            "name": name,
            "path": child_path,
            "type": "dir" if is_dir else "file",
        }

    return [children[name] for name in sorted(children.keys())]

@app.route("/api/snapshot/<snapshot_id>/tree/root")
def snapshot_tree_root_api(snapshot_id):
    """
    Return the top-level entries of the snapshot (lazy root).
    """
    restic = ResticUI()
    entries = restic.get_snapshot_contents(snapshot_id)
    children = _list_children(entries, parent_path="")
    return jsonify({"entries": children})

@app.route("/api/snapshot/<snapshot_id>/tree/node")
def snapshot_tree_node_api(snapshot_id):
    """
    Return the direct children of a given directory path within the snapshot.
    Query param: ?path=<dir_path>
    """
    parent_path = request.args.get("path", "", type=str)
    if parent_path is None:
        abort(400, description="Missing 'path' parameter")

    restic = ResticUI()
    entries = restic.get_snapshot_contents(snapshot_id)
    children = _list_children(entries, parent_path=parent_path)
    return jsonify({"entries": children})

if __name__ == "__main__":
    app.run(host="100.69.69.69")
