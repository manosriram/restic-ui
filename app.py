import time
import subprocess
import tempfile
import os
import threading
import json
from datetime import datetime
from math import ceil
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, abort, url_for

from restic import ResticUI

app = Flask(__name__)

# Single shared ResticUI instance so its internal cache is reused
restic = ResticUI()

SNAPSHOTS_PER_PAGE = 20

# Directory on the host where restore logs will be written
LOG_DIR = Path("/fs/containers/restic-ui/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/")
def default_route():
    # Use ResticUI's own cached snapshots to avoid repeated CLI calls
    all_snapshots = restic.get_snapshots() or []

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
        # Only compute/attach parsed time once per snapshot object
        if "_parsed_time" not in snap:
            dt = parse_time(snap.get("time"))
            snap["_parsed_time"] = dt
            if dt is not None:
                snap["_human_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                snap["_human_time"] = snap.get("time", "")

        # Normalize tags for display (restic uses "tags": ["a","b"] or may omit)
        if "_tags_display" not in snap:
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


def _run_restore(snapshot_id, restore_path, selected_paths, log_path: Path, status_path: Path):
    """
    Run a single restic restore command for all selected paths.

    - For a small number of paths, use multiple --include flags.
    - For many paths, write them to a temporary file and use --files-from.

    Logs are written directly to log_path, and status_path is updated with:
      - "running" at start
      - "success" on success
      - "error" on failure
    """
    restore_path = restore_path.strip()
    if not restore_path:
        raise ValueError("Empty restore_path")

    FILES_FROM_THRESHOLD = 100

    if len(selected_paths) == 0:
        raise ValueError("No paths selected for restore")

    # Mark as running
    try:
        status_path.write_text("running", encoding="utf-8")
    except Exception:
        app.logger.warning("Failed to write running status to %s", status_path)

    def _run_and_log(cmd, context):
        app.logger.info("Running restore command%s: %s", context, " ".join(cmd))
        with open(log_path, "ab", buffering=0) as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            returncode = proc.wait()
        if returncode != 0:
            app.logger.error("restic restore failed%s with code %s", context, returncode)
            return False
        return True

    success = False
    tmp_path = None

    try:
        if len(selected_paths) <= FILES_FROM_THRESHOLD:
            cmd = ["/usr/bin/restic", "restore", snapshot_id, "--target", restore_path]
            for path in selected_paths:
                cmd.extend(["--include", path.strip()])
            success = _run_and_log(cmd, " (includes)")
        else:
            try:
                with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                    tmp_path = f.name
                    for p in selected_paths:
                        f.write(p.strip() + "\n")
                cmd = [
                    "/usr/bin/restic",
                    "restore",
                    snapshot_id,
                    "--target",
                    restore_path,
                    "--files-from",
                    tmp_path,
                ]
                success = _run_and_log(cmd, " (files-from)")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        app.logger.warning(
                            "Failed to remove temporary files-from list: %s", tmp_path
                        )
    finally:
        try:
            status_path.write_text("success" if success else "error", encoding="utf-8")
        except Exception:
            app.logger.warning("Failed to write final status to %s", status_path)


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


@app.route("/api/restore/<job_id>/logs")
def restore_logs_api(job_id):
    """
    Return current logs and status for a given restore job.

    Response JSON:
      {
        "logs": "<full log file contents or empty string>",
        "status": "pending" | "running" | "success" | "error"
      }
    """
    # Basic sanity: avoid path traversal
    if "/" in job_id or ".." in job_id:
        abort(400)

    log_path = LOG_DIR / f"{job_id}.log"
    status_path = LOG_DIR / f"{job_id}.status"

    logs = ""
    status = "pending"

    if log_path.exists():
        try:
            logs = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            app.logger.warning("Failed to read log file %s: %s", log_path, e)

    if status_path.exists():
        try:
            status = status_path.read_text(encoding="utf-8", errors="replace").strip() or "pending"
        except Exception as e:
            app.logger.warning("Failed to read status file %s: %s", status_path, e)

    return jsonify({"logs": logs, "status": status})


@app.route("/snapshot/<snapshot_id>", methods=["GET", "POST"])
def snapshot_detail(snapshot_id):
    restore_status = None
    restore_logs = ""
    job_id = None

    if request.method == "POST":
        selected_paths = request.form.getlist("selected_paths")
        restore_path = request.form.get("restore_path", "").strip()
        if selected_paths and restore_path:
            # Create a simple job id
            job_id = f"{snapshot_id}-{int(time.time())}"
            log_path = LOG_DIR / f"{job_id}.log"
            status_path = LOG_DIR / f"{job_id}.status"

            # Ensure empty log file and initial status
            try:
                log_path.write_text("", encoding="utf-8")
            except Exception as e:
                app.logger.error("Failed to create log file %s: %s", log_path, e)
            try:
                status_path.write_text("pending", encoding="utf-8")
            except Exception as e:
                app.logger.error("Failed to create status file %s: %s", status_path, e)

            # Start background thread to run restore
            thread = threading.Thread(
                target=_run_restore,
                args=(snapshot_id, restore_path, selected_paths, log_path, status_path),
                daemon=True,
            )
            thread.start()

            restore_status = "started"
            restore_logs = "Restore started. Logs will appear below."

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
        .restore-log-box {
          margin-top: 0.5rem;
          border: 1px solid #666;
          padding: 0.5rem;
          max-height: 200px;
          overflow-y: auto;
          font-family: monospace;
          font-size: 0.9rem;
          white-space: pre-wrap;
          background-color: #111;
          color: white;
        }
        .restore-log-box.hidden {
          display: none;
        }
        .terminal-alert-success {
          border-color: #00ff00 !important;
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

            <form id="restore-form" method="post" onsubmit="return onRestoreSubmit(event)">
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
                    {% if restore_status == 'started' %}
                      <div class="terminal-alert terminal-alert-primary">
                        Restore started
                      </div>
                    {% endif %}
                  </div>
                  <div
                    id="restore-log"
                    class="restore-log-box {% if not restore_logs %}hidden{% endif %}"
                  >{{ restore_logs }}</div>
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

        function confirmSelectionAndPath() {
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

        async function onRestoreSubmit(event) {
          event.preventDefault();
          if (!confirmSelectionAndPath()) {
            return false;
          }

          const form = document.getElementById('restore-form');
          const formData = new FormData(form);

          const statusEl = document.getElementById("restore-status");
          const logEl = document.getElementById("restore-log");
          if (statusEl) {
            statusEl.innerHTML =
              '<div class="terminal-alert terminal-alert-primary">Restore starting...</div>';
          }
          if (logEl) {
            logEl.classList.remove('hidden');
            logEl.textContent = 'Starting restic restore...';
          }

          try {
            const response = await fetch(window.location.href, {
              method: 'POST',
              body: formData,
            });
            const text = await response.text();

            // Parse the returned HTML to extract job_id if present
            const parser = new DOMParser();
            const doc = parser.parseFromString(text, 'text/html');
            const jobMeta = doc.querySelector('meta[name="restore-job-id"]');
            const jobId = jobMeta ? jobMeta.getAttribute('content') : null;

            document.documentElement.replaceWith(doc.documentElement);

            if (jobId) {
              startLogPolling(jobId);
            }
          } catch (e) {
            alert("Error starting restore: " + e);
          }

          return false;
        }

        let logPollInterval = null;

        function startLogPolling(jobId) {
          if (!jobId) return;
          if (logPollInterval) {
            clearInterval(logPollInterval);
          }

          const statusEl = document.getElementById("restore-status");
          const logEl = document.getElementById("restore-log");
          if (statusEl) {
            statusEl.innerHTML =
              '<div class="terminal-alert terminal-alert-primary">Restore in progress</div>';
          }
          if (logEl) {
            logEl.classList.remove('hidden');
          }

          async function pollOnce() {
            try {
              const url = '{{ url_for("restore_logs_api", job_id="__JOB_ID__") }}'.replace('__JOB_ID__', encodeURIComponent(jobId));
              const response = await fetch(url);
              if (!response.ok) {
                return;
              }
              const data = await response.json();
              if (logEl) {
                logEl.textContent = data.logs || '';
                logEl.scrollTop = logEl.scrollHeight;
              }
              if (statusEl) {
                if (data.status === 'success') {
                  statusEl.innerHTML =
                    '<div class="terminal-alert terminal-alert-primary terminal-alert-success">Restore completed</div>';
                } else if (data.status === 'error') {
                  statusEl.innerHTML =
                    '<div class="terminal-alert terminal-alert-error">Restore failed</div>';
                } else if (data.status === 'running' || data.status === 'pending') {
                  statusEl.innerHTML =
                    '<div class="terminal-alert terminal-alert-primary">Restore in progress</div>';
                }
              }
              if (data.status === 'success' || data.status === 'error') {
                clearInterval(logPollInterval);
                logPollInterval = null;
              }
            } catch (e) {
              // ignore transient errors
            }
          }

          pollOnce();
          logPollInterval = setInterval(pollOnce, 2000);
        }

        document.addEventListener('DOMContentLoaded', () => {
          loadRoot();

          // If the server rendered a job_id (e.g. after POST), start polling
          const jobMeta = document.querySelector('meta[name="restore-job-id"]');
          if (jobMeta) {
            const jobId = jobMeta.getAttribute('content');
            if (jobId) {
              startLogPolling(jobId);
            }
          }
        });
      </script>

      {% if job_id %}
      <meta name="restore-job-id" content="{{ job_id }}">
      {% endif %}
    </body>
    </html>
    """
    return render_template_string(
        template,
        snapshot_id=snapshot_id,
        restore_status=restore_status,
        restore_logs=restore_logs,
        job_id=job_id,
    )


if __name__ == "__main__":
    app.run(host="100.69.69.69")
