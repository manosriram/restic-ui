import time
import subprocess
import tempfile
import os
from datetime import datetime
from math import ceil
from flask import Flask, render_template_string, request, jsonify, abort, url_for

from restic import ResticUI

app = Flask(__name__)

# Single shared ResticUI instance so its internal cache is reused
restic = ResticUI()

SNAPSHOTS_PER_PAGE = 20


@app.route("/")
def default_route():
    # Cache snapshots in memory for 10 seconds to reduce CLI calls
    if not hasattr(restic, "_cached_snapshots") or (getattr(restic, "_cache_time", 0) + 10) < time.time():
        restic._cached_snapshots = restic.get_snapshots()
        restic._cache_time = time.time()
    all_snapshots = restic._cached_snapshots or []

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

    for snap in all_snapshots:
        dt = parse_time(snap.get("time"))
        snap["_parsed_time"] = dt
        if dt is not None:
            snap["_human_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            snap["_human_time"] = snap.get("time", "")

        # Normalize tags for display (restic uses "tags": ["a","b"] or may omit)
        tags = snap.get("tags") or []
        if isinstance(tags, list):
            snap["_tags_display"] = ", ".join(tags)
        else:
            snap["_tags_display"] = str(tags)

    all_snapshots.sort(key=lambda s: s.get("_parsed_time") or datetime.min, reverse=True)

    # Pagination
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    total = len(all_snapshots)
    per_page = SNAPSHOTS_PER_PAGE
    total_pages = max(1, ceil(total / per_page)) if total else 1
    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    snapshots = all_snapshots[start:end]

    template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Restic Snapshots</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="stylesheet" href="https://unpkg.com/terminal.css@0.7.4/dist/terminal.min.css" />
      <style>
        /* Make the main container wider than the default Terminal CSS width */
        .wide-container {
          max-width: 1200px;
          margin: 0 auto;
        }
        /* Make the table a bit wider and allow horizontal scrolling if needed */
        .snapshots-wrapper {
          max-width: 100%;
          overflow-x: auto;
        }
        .snapshots-table {
          min-width: 70rem; /* increase logical width of the table */
        }
        .pagination {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.75rem;
        }
        .pagination span {
          display: inline-block;
        }
      </style>
    </head>
    <body class="terminal">
      <div class="container wide-container">
        <header>
          <div class="terminal-nav">
            <div class="terminal-logo">
              <a href="/">Restic UI</a>
            </div>
            <nav class="terminal-menu">
              <ul>
                <li><a href="/">Snapshots</a></li>
              </ul>
            </nav>
          </div>
        </header>

        <main>
          <section>
            <header>
              <h1>Snapshots</h1>
              <p>A list of available restic snapshots.</p>
            </header>

            {% if snapshots %}
              <div class="snapshots-wrapper">
                <table class="snapshots-table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Time</th>
                      <th>Host</th>
                      <th>Tags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for snap in snapshots %}
                      <tr>
                        <td>
                          <a href="/snapshot/{{ snap.id }}">{{ snap.id }}</a>
                        </td>
                        <td>{{ snap._human_time }}</td>
                        <td>{{ snap.hostname }}</td>
                        <td>{{ snap._tags_display }}</td>
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>

              <div class="pagination">
                {% if page > 1 %}
                  <a href="{{ url_for('default_route', page=page-1) }}">&larr; Previous</a>
                {% else %}
                  <span>&larr; Previous</span>
                {% endif %}

                <span>Page {{ page }} of {{ total_pages }}</span>

                {% if page < total_pages %}
                  <a href="{{ url_for('default_route', page=page+1) }}">Next &rarr;</a>
                {% else %}
                  <span>Next &rarr;</span>
                {% endif %}
              </div>
            {% else %}
              <div class="terminal-alert">
                No snapshots found.
              </div>
            {% endif %}
          </section>
        </main>
      </div>
    </body>
    </html>
    """
    return render_template_string(
        template,
        snapshots=snapshots,
        page=page,
        total_pages=total_pages,
    )


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
            "restic",
            "restore",
            snapshot_id,
            "--target",
            restore_path,
            "--files-from",
            tmp_path,
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
                app.logger.warning(
                    "Failed to remove temporary files-from list: %s", tmp_path
                )


def _build_tree(entries):
    """
    Build a simple directory tree index from flat restic entries.

    Returns a dict:
      {
        "": [ {name, path, type}, ... ],          # root entries
        "some/dir": [ {name, path, type}, ... ],  # children of 'some/dir'
        ...
      }
    """
    tree = {}
    for e in entries:
        path = e["path"]
        parts = path.split("/")
        if len(parts) == 1:
            parent = ""
            name = parts[0]
        else:
            parent = "/".join(parts[:-1])
            name = parts[-1]
        node_type = e.get("type") or "file"
        tree.setdefault(parent, []).append(
            {"name": name, "path": path, "type": node_type}
        )
    return tree


@app.route("/api/snapshot/<snapshot_id>/tree-root")
def snapshot_tree_root_api(snapshot_id):
    """
    Return the top-level entries for a snapshot.
    """
    entries = restic.get_snapshot_contents(snapshot_id)
    if entries is None:
        abort(404)
    tree = _build_tree(entries)
    root_entries = tree.get("", [])
    # Sort directories first, then files, then by name
    root_entries.sort(
        key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower())
    )
    return jsonify({"entries": root_entries})


@app.route("/api/snapshot/<snapshot_id>/tree-node")
def snapshot_tree_node_api(snapshot_id):
    """
    Return the direct children of a given directory path within a snapshot.
    Query param: ?path=<dir_path>
    """
    dir_path = request.args.get("path", "").strip()
    entries = restic.get_snapshot_contents(snapshot_id)
    if entries is None:
        abort(404)
    tree = _build_tree(entries)
    children = tree.get(dir_path, [])
    children.sort(
        key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower())
    )
    return jsonify({"entries": children})


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
                app.logger.error(
                    "Restore error for snapshot %s: %s", snapshot_id, e
                )
                restore_status = "error"

    template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Snapshot {{ snapshot_id }}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="stylesheet" href="https://unpkg.com/terminal.css@0.7.4/dist/terminal.min.css" />
      <style>
        .wide-container {
          max-width: 1200px;
          margin: 0 auto;
        }
        .nested {
          margin-left: 1.2em;
        }
        .tree-loading {
          font-style: italic;
        }
        .dir-label {
          text-decoration: underline;
          cursor: pointer;
        }
        .restore-controls {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.5rem;
        }
        .restore-controls input[type="text"] {
          flex: 1 1 auto;
        }
      </style>
    </head>
    <body class="terminal">
      <div class="container wide-container">
        <header>
          <div class="terminal-nav">
            <div class="terminal-logo">
              <a href="/">Restic UI</a>
            </div>
            <nav class="terminal-menu">
              <ul>
                <li><a href="/">Snapshots</a></li>
              </ul>
            </nav>
          </div>
        </header>

        <main>
          <section>
            <header>
              <h1>Snapshot</h1>
              <p>
                Snapshot ID:
                {{ snapshot_id }}
              </p>
            </header>

            <form id="restore-form" method="post" onsubmit="return confirmRestore()">
              <fieldset>
                <legend>Restore options</legend>

                <div>
                  <label>Snapshot contents:</label>
                  <div id="tree-container" class="tree-loading">
                    Loading snapshot contents...
                  </div>
                </div>

                <div class="restore-controls">
                  <label for="restore_path" style="margin: 0;">Restore path:</label>
                  <input
                    type="text"
                    id="restore_path"
                    name="restore_path"
                    required
                    placeholder="/path/to/restore"
                  />
                  <button type="submit">Restore selected</button>
                </div>
                <small>Files and directories will be restored under this path.</small>

                <div style="margin-top: 0.5rem;">
                  <div id="restore-status">
                    {% if restore_status == 'completed' %}
                      <div class="terminal-alert terminal-alert-primary">
                        Restore completed
                      </div>
                    {% elif restore_status == 'error' %}
                      <div class="terminal-alert terminal-alert-error">
                        Restore failed
                      </div>
                    {% endif %}
                  </div>
                </div>
              </fieldset>
            </form>

            <p>
              <a href="/">&larr; Back to snapshots</a>
            </p>
          </section>
        </main>
      </div>

      <script>
        async function loadRoot() {
          const container = document.getElementById('tree-container');
          container.classList.add('tree-loading');
          container.textContent = 'Loading snapshot contents...';
          try {
            const response = await fetch('{{ url_for("snapshot_tree_root_api", snapshot_id=snapshot_id) }}');
            if (!response.ok) {
              container.classList.remove('tree-loading');
              container.innerHTML = '<div class="terminal-alert terminal-alert-error">Error loading snapshot contents.</div>';
              return;
            }
            const data = await response.json();
            container.classList.remove('tree-loading');
            container.innerHTML = buildNodeListHtml(data.entries, '');
          } catch (e) {
            container.classList.remove('tree-loading');
            container.innerHTML = '<div class="terminal-alert terminal-alert-error">Error loading snapshot contents.</div>';
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
          let html = '<ul>';
          for (const entry of entries) {
            const fullPath = entry.path;
            const safeId = escapeHtml(fullPath).replace(/[^a-zA-Z0-9_-]/g, '_');
            const safeLabel = escapeHtml(entry.name);
            const checkbox =
              '<input type="checkbox" name="selected_paths" value="' + escapeHtml(fullPath) + '" id="' + safeId + '"> ';

            if (entry.type === 'dir') {
              // Directory: label is clickable/underlined and toggles children
              const label =
                '<span class="dir-label" data-path="' + escapeHtml(fullPath) + '" onclick="onDirLabelClick(this)">' +
                safeLabel +
                '</span>';

              html += '<li>';
              html += checkbox + label;
              html += '<div class="nested" style="display:none;" data-loaded="false"></div>';
              html += '</li>';
            } else {
              const label =
                '<label for="' + safeId + '">' + safeLabel + '</label>';
              html += '<li>' + checkbox + label + '</li>';
            }
          }
          html += '</ul>';
          return html;
        }

        async function onDirLabelClick(labelEl) {
          const parentLi = labelEl.parentElement;
          const nested = parentLi.querySelector('.nested');
          const path = labelEl.getAttribute('data-path');
          const loaded = nested.getAttribute('data-loaded') === 'true';

          // Toggle visibility
          if (nested.style.display === 'none' || nested.style.display === '') {
            nested.style.display = 'block';
          } else if (loaded) {
            nested.style.display = 'none';
            return;
          }

          if (loaded) {
            return;
          }

          nested.innerHTML = '<span class="tree-loading">Loading...</span>';
          try {
            const url = new URL('{{ url_for("snapshot_tree_node_api", snapshot_id=snapshot_id) }}', window.location.origin);
            url.searchParams.set('path', path);
            const response = await fetch(url.toString());
            if (!response.ok) {
              nested.innerHTML = '<div class="terminal-alert terminal-alert-error">Error loading directory.</div>';
              return;
            }
            const data = await response.json();
            nested.innerHTML = buildNodeListHtml(data.entries, path);
            nested.setAttribute('data-loaded', 'true');
          } catch (e) {
            nested.innerHTML = '<div class="terminal-alert terminal-alert-error">Error loading directory.</div>';
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

          const statusEl = document.getElementById("restore-status");
          if (statusEl) {
            statusEl.innerHTML =
              '<div class="terminal-alert terminal-alert-primary">Restore in progress</div>';
          }

          return confirm(`Restore ${checked.length} item(s) to "${path}"?`);
        }

        document.addEventListener('DOMContentLoaded', loadRoot);
      </script>
    </body>
    </html>
    """
    return render_template_string(
        template, snapshot_id=snapshot_id, restore_status=restore_status
    )


if __name__ == "__main__":
    app.run(host="100.69.69.69")
