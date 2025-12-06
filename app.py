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
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Restic Snapshots</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-100 text-slate-900">
      <div class="min-h-screen flex flex-col">
        <header class="bg-slate-900 text-white">
          <div class="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
            <h1 class="text-xl font-semibold">Restic UI</h1>
            <span class="text-xs text-slate-300">Snapshots</span>
          </div>
        </header>

        <main class="flex-1">
          <div class="max-w-5xl mx-auto px-4 py-6">
            <div class="bg-white shadow-sm rounded-lg border border-slate-200">
              <div class="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
                <h2 class="text-lg font-medium text-slate-900">Snapshots</h2>
              </div>
              <div class="p-4">
                {% if snapshots %}
                  <div class="overflow-x-auto">
                    <table class="min-w-full text-sm">
                      <thead>
                        <tr class="border-b border-slate-200 text-left text-slate-500">
                          <th class="py-2 pr-4 font-medium">ID</th>
                          <th class="py-2 pr-4 font-medium">Time</th>
                          <th class="py-2 pr-4 font-medium">Host</th>
                        </tr>
                      </thead>
                      <tbody class="divide-y divide-slate-100">
                        {% for snap in snapshots %}
                          <tr class="hover:bg-slate-50">
                            <td class="py-2 pr-4 font-mono text-xs">
                              <a href="/snapshot/{{ snap.id }}" class="text-sky-600 hover:text-sky-800 underline decoration-sky-300">
                                {{ snap.id }}
                              </a>
                            </td>
                            <td class="py-2 pr-4 text-slate-800">
                              {{ snap._human_time }}
                            </td>
                            <td class="py-2 pr-4 text-slate-700">
                              {{ snap.hostname }}
                            </td>
                          </tr>
                        {% endfor %}
                      </tbody>
                    </table>
                  </div>
                {% else %}
                  <p class="text-sm text-slate-600">No snapshots found.</p>
                {% endif %}
              </div>
            </div>
          </div>
        </main>
      </div>
    </body>
    </html>
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
    root_entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
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
    children.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
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
                app.logger.error("Restore error for snapshot %s: %s", snapshot_id, e)
                restore_status = "error"

    template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Snapshot {{ snapshot_id }}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <script src="https://cdn.tailwindcss.com"></script>
      <style>
        .caret {
          cursor: pointer;
          user-select: none;
          display: inline-block;
          margin-left: 6px;
          color: rgb(30 64 175); /* tailwind blue-800 */
        }
        .caret::before {
          content: "\\25B6"; /* right-pointing triangle */
          display: inline-block;
          transform: rotate(0deg);
          transition: transform 0.15s ease;
        }
        .caret-down::before {
          transform: rotate(90deg);
        }
        .nested {
          margin-left: 0.75rem;
        }
      </style>
    </head>
    <body class="bg-slate-100 text-slate-900">
      <div class="min-h-screen flex flex-col">
        <header class="bg-slate-900 text-white">
          <div class="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
            <div class="flex items-center gap-3">
              <a href="/" class="text-xs text-slate-300 hover:text-white">&larr; Back</a>
              <h1 class="text-lg font-semibold">Snapshot</h1>
            </div>
            <code class="text-[10px] bg-slate-800 px-2 py-1 rounded border border-slate-700">
              {{ snapshot_id }}
            </code>
          </div>
        </header>

        <main class="flex-1">
          <div class="max-w-5xl mx-auto px-4 py-6 space-y-4">
            <div class="bg-white shadow-sm rounded-lg border border-slate-200">
              <div class="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
                <h2 class="text-sm font-medium text-slate-900">Restore</h2>
                <span class="text-[11px] text-slate-500">Select paths and target directory</span>
              </div>

              <form id="restore-form" method="post" onsubmit="return confirmRestore()">
                <div class="p-4 space-y-4">
                  <div>
                    <label for="restore_path" class="block text-xs font-medium text-slate-700 mb-1">
                      Restore path
                    </label>
                    <input
                      type="text"
                      id="restore_path"
                      name="restore_path"
                      required
                      placeholder="/path/to/restore"
                      class="w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-sky-500 focus:border-sky-500"
                    >
                    <p class="mt-1 text-[11px] text-slate-500">
                      Files and directories will be restored under this path.
                    </p>
                  </div>

                  <div>
                    <div class="flex items-center justify-between mb-2">
                      <label class="text-xs font-medium text-slate-700">
                        Snapshot contents
                      </label>
                      <span id="tree-loading-label" class="text-[11px] text-slate-500">Loading…</span>
                    </div>
                    <div
                      id="tree-container"
                      class="rounded-md border border-slate-200 bg-slate-50 max-h-[420px] overflow-auto text-xs p-2"
                    >
                      Loading snapshot contents...
                    </div>
                  </div>

                  <div class="flex items-center justify-between pt-2 border-t border-slate-200">
                    <div id="restore-status" class="text-[11px] text-slate-500">
                      {% if restore_status == 'completed' %}
                        <span class="text-emerald-700 font-medium">Restore completed</span>
                      {% elif restore_status == 'error' %}
                        <span class="text-red-600 font-medium">Restore failed</span>
                      {% endif %}
                    </div>
                    <button
                      type="submit"
                      class="inline-flex items-center gap-1 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-sky-700 focus:outline-none focus:ring-2 focus:ring-sky-500 focus:ring-offset-1"
                    >
                      Restore selected
                    </button>
                  </div>
                </div>
              </form>
            </div>
          </div>
        </main>
      </div>

      <script>
        async function loadRoot() {
          const container = document.getElementById('tree-container');
          const loadingLabel = document.getElementById('tree-loading-label');
          container.classList.add('text-slate-500', 'italic');
          container.textContent = 'Loading snapshot contents...';
          try {
            const response = await fetch('{{ url_for("snapshot_tree_root_api", snapshot_id=snapshot_id) }}');
            if (!response.ok) {
              container.classList.remove('italic');
              container.classList.add('text-red-600');
              container.textContent = 'Error loading snapshot contents.';
              if (loadingLabel) loadingLabel.textContent = 'Error';
              return;
            }
            const data = await response.json();
            container.classList.remove('italic');
            container.classList.remove('text-slate-500');
            container.innerHTML = buildNodeListHtml(data.entries, '');
            if (loadingLabel) loadingLabel.textContent = 'Loaded';
          } catch (e) {
            container.classList.remove('italic');
            container.classList.add('text-red-600');
            container.textContent = 'Error loading snapshot contents.';
            if (loadingLabel) loadingLabel.textContent = 'Error';
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
          let html = '<ul class="space-y-0.5">';
          for (const entry of entries) {
            const fullPath = entry.path;
            const safeId = escapeHtml(fullPath).replace(/[^a-zA-Z0-9_-]/g, '_');
            const safeLabel = escapeHtml(entry.name);
            const checkbox =
              '<input type="checkbox" class="mr-1 align-middle rounded border-slate-300 text-sky-600 focus:ring-sky-500" ' +
              'name="selected_paths" value="' + escapeHtml(fullPath) + '" id="' + safeId + '">';
            const label =
              '<label for="' + safeId + '" class="cursor-pointer align-middle text-slate-800">' +
              safeLabel + '</label>';

            if (entry.type === 'dir') {
              html += '<li class="flex flex-col">';
              html += '<div class="flex items-center gap-1">';
              html += checkbox + label;
              html += '<span class="caret text-[10px]" data-path="' + escapeHtml(fullPath) + '" data-loaded="false" onclick="onCaretClick(this)"></span>';
              html += '</div>';
              html += '<div class="nested ml-4 mt-0.5" style="display:none;"></div>';
              html += '</li>';
            } else {
              html += '<li class="flex items-center gap-1">';
              html += checkbox + label;
              html += '</li>';
            }
          }
          html += '</ul>';
          return html;
        }

        async function onCaretClick(element) {
          const nested = element.nextElementSibling;
          const path = element.getAttribute('data-path');
          const loaded = element.getAttribute('data-loaded') === 'true';

          if (nested.style.display === 'none') {
            nested.style.display = 'block';
            element.classList.add('caret-down');
          } else if (loaded) {
            nested.style.display = 'none';
            element.classList.remove('caret-down');
            return;
          }

          if (loaded) {
            return;
          }

          nested.innerHTML = '<span class="text-slate-500 italic text-[11px]">Loading...</span>';
          try {
            const url = new URL('{{ url_for("snapshot_tree_node_api", snapshot_id=snapshot_id) }}', window.location.origin);
            url.searchParams.set('path', path);
            const response = await fetch(url.toString());
            if (!response.ok) {
              nested.innerHTML = '<span class="text-red-600 text-[11px]">Error loading directory.</span>';
              return;
            }
            const data = await response.json();
            nested.innerHTML = buildNodeListHtml(data.entries, path);
            element.setAttribute('data-loaded', 'true');
          } catch (e) {
            nested.innerHTML = '<span class="text-red-600 text-[11px]">Error loading directory.</span>';
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
            statusEl.innerHTML = '<span class="text-sky-700 font-medium">Restore in progress</span>';
          }

          return confirm(`Restore ${checked.length} item(s) to "${path}"?`);
        }

        document.addEventListener('DOMContentLoaded', loadRoot);
      </script>
    </body>
    </html>
    """
    return render_template_string(template, snapshot_id=snapshot_id, restore_status=restore_status)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
