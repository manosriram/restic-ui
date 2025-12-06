import time
import subprocess
import tempfile
import os
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, abort

from restic import ResticUI

app = Flask(__name__)

# Single shared ResticUI instance so its internal cache is reused
restic = ResticUI()

@app.route("/")
def default_route():
    # Cache snapshots in memory for 10 seconds to reduce CLI calls
    if not hasattr(restic, "_cached_snapshots") or (restic._cache_time + 10) < time.time():
        restic._cached_snapshots = restic.get_snapshots()
        restic._cache_time = time.time()
    snapshots = restic._cached_snapshots or []

    # Sort snapshots by time descending and format timestamp
    def parse_time(s):
        # restic uses RFC3339, e.g. "2023-09-01T12:34:56.123456789Z"
        # We strip sub-second precision and trailing Z for parsing.
        if not s:
            return None
        t = s
        if t.endswith("Z"):
            t = t[:-1]
        if "." in t:
            t = t.split(".", 1)[0]
        try:
            return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    for snap in snapshots:
        dt = parse_time(snap.get("time"))
        snap["_parsed_time"] = dt
        if dt is not None:
            snap["_human_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            snap["_human_time"] = snap.get("time", "")

    snapshots.sort(key=lambda s: s.get("_parsed_time") or datetime.min, reverse=True)

    template = """
    <h1>Restic Snapshots</h1>
    {% if snapshots %}
    <ul>
    {% for snap in snapshots %}
      <li>
        <a href="/snapshot/{{ snap.id }}">{{ snap.id }}</a>
        - {{ snap._human_time }}
        - {{ snap.hostname }}
      </li>
    {% endfor %}
    </ul>
    {% else %}
    <p>No snapshots found.</p>
    {% endif %}
    """
    return render_template_string(template, snapshots=snapshots)

def _run_restore(snapshot_id, restore_path, selected_paths):
    """
    Run a single restic restore command for all selected paths.

    - For a small number of paths, use multiple --include flags.
    - For many paths, write them to a temporary file and use --files-from.
    """
    # Basic sanity checks; you can tighten these as needed
    restore_path = restore_path.strip()
    if not restore_path:
        raise ValueError("Empty restore_path")

    # Threshold for switching to --files-from
    FILES_FROM_THRESHOLD = 100

    if len(selected_paths) == 0:
        raise ValueError("No paths selected for restore")

    if len(selected_paths) <= FILES_FROM_THRESHOLD:
        # Use multiple --include flags
        cmd = ["restic", "restore", snapshot_id, "--target", restore_path]
        for path in selected_paths:
            # Paths come from our own UI, but still strip whitespace
            cmd.extend(["--include", path.strip()])
        app.logger.info("Running restore command: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            app.logger.error("restic restore failed (includes): %s", result.stderr)
            raise RuntimeError(f"restic restore failed: {result.stderr}")
        return

    # Many paths: use --files-from
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            tmp_path = f.name
            for p in selected_paths:
                f.write(p.strip() + "\n")

        cmd = [
            "restic", "restore", snapshot_id,
            "--target", restore_path,
            "--files-from", tmp_path,
        ]
        app.logger.info("Running restore command with files-from: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            app.logger.error("restic restore failed (files-from): %s", result.stderr)
            raise RuntimeError(f"restic restore failed: {result.stderr}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                app.logger.warning("Failed to remove temporary files-from list: %s", tmp_path)

@app.route("/snapshot/<snapshot_id>", methods=["GET", "POST"])
def snapshot_detail(snapshot_id):
    restore_status = None

    if request.method == "POST":
        selected_paths = request.form.getlist("selected_paths")
        restore_path = request.form.get("restore_path", "").strip()
        if selected_paths and restore_path:
            try:
                _run_restore(snapshot_id, restore_path, selected_paths)
                restore_status = "completed"
            except Exception as e:
                app.logger.error("Restore error for snapshot %s: %s", snapshot_id, e)
                restore_status = "error"

    # Initial page render or after POST: we don't build the whole tree here anymore
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
      #restore-status {
        margin-top: 0.5em;
        font-style: italic;
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
      <div id="restore-status">
        {% if restore_status == 'completed' %}
          Restore completed
        {% elif restore_status == 'error' %}
          Restore failed
        {% endif %}
      </div>
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

        // Show "Restore in progress" immediately
        const statusEl = document.getElementById("restore-status");
        if (statusEl) {
          statusEl.textContent = "Restore in progress";
        }

        return confirm(`Restore ${checked.length} item(s) to "${path}"?`);
      }

      document.addEventListener('DOMContentLoaded', loadRoot);
    </script>
    """
    return render_template_string(template, snapshot_id=snapshot_id, restore_status=restore_status)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
