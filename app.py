import time
from flask import Flask, render_template_string

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

from flask import request, redirect, url_for
import subprocess

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

    contents = restic.get_snapshot_contents(snapshot_id)

    tree = {}
    for line in contents:
        parts = line.strip().split()
        path = parts[-1] if parts else ""
        if not path:
            continue
        # Skip very deep paths to avoid huge trees causing hangs
        segments = path.split('/')
        if len(segments) > 20:
            continue
        current = tree
        for segment in segments:
            current = current.setdefault(segment, {})

    def render_tree(d, prefix=""):
        html = '<ul style="list-style-type:none; padding-left: 1em;">'
        for key, subtree in sorted(d.items()):
            full_path = f"{prefix}/{key}" if prefix else key
            if subtree:
                html += (
                    f'<li>'
                    f'<input type="checkbox" name="selected_paths" value="{full_path}" id="{full_path}">'
                    f'<label for="{full_path}">{key}</label> '
                    f'<span class="caret" onclick="toggleNested(this)"></span>'
                    f'<div class="nested" style="display:none;">{render_tree(subtree, full_path)}</div>'
                    f'</li>'
                )
            else:
                html += (
                    f'<li>'
                    f'<input type="checkbox" name="selected_paths" value="{full_path}" id="{full_path}">'
                    f'<label for="{full_path}">{key}</label>'
                    f'</li>'
                )
        html += "</ul>"
        return html

    tree_html = render_tree(tree)

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
    {% if tree_html %}
      <form method="post" onsubmit="return confirmRestore()">
        {{ tree_html|safe }}
        <p>
          <label for="restore_path">Restore Path:</label>
          <input type="text" id="restore_path" name="restore_path" required placeholder="/path/to/restore">
        </p>
        <button type="submit">Restore Selected</button>
      </form>
    {% else %}
      <p>No contents found or error retrieving snapshot.</p>
    {% endif %}
    <p><a href="/">Back to snapshots</a></p>
    <script>
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
    </script>
    """
    return render_template_string(template, snapshot_id=snapshot_id, tree_html=tree_html)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
