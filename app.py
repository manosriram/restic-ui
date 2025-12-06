import time
import subprocess
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

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
        transition: transform 0.3s ease;
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
    </style>

    <form id="restore-form" method="post" onsubmit="return confirmRestore()">
      <div id="tree-container">
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
      async function loadTree() {
        const container = document.getElementById('tree-container');
        try {
          const response = await fetch('{{ url_for("snapshot_tree_api", snapshot_id=snapshot_id) }}');
          if (!response.ok) {
            container.textContent = 'Error loading snapshot contents.';
            return;
          }
          const data = await response.json();
          container.innerHTML = buildTreeHtml(data.tree, '');

          if (data.truncated) {
            const warning = document.createElement('p');
            warning.style.color = 'red';
            warning.textContent = 'Warning: tree truncated due to size limits; some entries may be missing.';
            container.prepend(warning);
          }
        } catch (e) {
          container.textContent = 'Error loading snapshot contents.';
        }
      }

      function buildTreeHtml(node, prefix) {
        let html = '<ul style="list-style-type:none; padding-left: 1em;">';
        const keys = Object.keys(node).sort();
        for (const key of keys) {
          const subtree = node[key];
          const fullPath = prefix ? (prefix + '/' + key) : key;
          if (Object.keys(subtree).length > 0) {
            html += '<li>'
              + '<input type="checkbox" name="selected_paths" value="' + fullPath + '" id="' + fullPath + '">'
              + '<label for="' + fullPath + '">' + key + '</label> '
              + '<span class="caret" onclick="toggleNested(this)"></span>'
              + '<div class="nested" style="display:none;">'
              + buildTreeHtml(subtree, fullPath)
              + '</div>'
              + '</li>';
          } else {
            html += '<li>'
              + '<input type="checkbox" name="selected_paths" value="' + fullPath + '" id="' + fullPath + '">'
              + '<label for="' + fullPath + '">' + key + '</label>'
              + '</li>';
          }
        }
        html += '</ul>';
        return html;
      }

      function toggleNested(element) {
        element.classList.toggle("caret-down");
        var nested = element.nextElementSibling;
        if (nested.style.display === "none") {
          nested.style.display = "block";
        } else {
          nested.style.display = "none";
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

      document.addEventListener('DOMContentLoaded', loadTree);
    </script>
    """
    return render_template_string(template, snapshot_id=snapshot_id)

@app.route("/api/snapshot/<snapshot_id>/tree")
def snapshot_tree_api(snapshot_id):
    """
    API endpoint that returns a directory tree for the snapshot.
    We still cap the number of entries processed to avoid pathological
    repositories, but we no longer truncate by depth or insert artificial
    path segments.
    """
    restic = ResticUI()
    contents = restic.get_snapshot_contents(snapshot_id)

    tree = {}
    # Raise this significantly so typical repos are fully represented.
    # If you want absolutely no cap, set max_entries = None and adjust the loop.
    max_entries = 200000  # hard cap on number of paths processed

    count = 0
    for line in contents:
        if max_entries is not None and count >= max_entries:
            break
        parts = line.strip().split()
        path = parts[-1] if parts else ""
        if not path:
            continue
        segments = path.split('/')
        current = tree
        for segment in segments:
            current = current.setdefault(segment, {})
        count += 1

    truncated = max_entries is not None and count >= max_entries
    return jsonify({"tree": tree, "truncated": truncated})

if __name__ == "__main__":
    app.run(host="100.69.69.69")
