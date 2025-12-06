from flask import Flask, render_template_string

from restic import ResticUI

app = Flask(__name__)

@app.route("/")
def default_route():
    snapshots = ResticUI().get_snapshots()
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

@app.route("/snapshot/<snapshot_id>")
def snapshot_detail(snapshot_id):
    restic = ResticUI()
    contents = restic.get_snapshot_contents(snapshot_id)

    # Build a tree structure from the flat list of paths
    tree = {}
    for line in contents:
        parts = line.strip().split()
        # The last part is the path, e.g. "path/to/file"
        path = parts[-1] if parts else ""
        if not path:
            continue
        segments = path.split('/')
        current = tree
        for segment in segments:
            current = current.setdefault(segment, {})

    def render_tree(d, level=0):
        html = '<ul style="list-style-type:none; padding-left: 1em;">'
        for key, subtree in sorted(d.items()):
            if subtree:
                html += (
                    f'<li>'
                    f'<span class="caret" onclick="toggleNested(this)">{key}</span>'
                    f'<div class="nested" style="display:none;">{render_tree(subtree, level+1)}</div>'
                    f'</li>'
                )
            else:
                html += f'<li>{key}</li>'
        html += "</ul>"
        return html

    tree_html = render_tree(tree)

    template = """
    <h1>Snapshot {{ snapshot_id }} Contents</h1>
    <style>
      .caret {
        cursor: pointer;
        user-select: none;
      }
      .caret::before {
        content: "\\25B6"; /* right-pointing triangle */
        color: black;
        display: inline-block;
        margin-right: 6px;
        transform: rotate(0deg);
        transition: transform 0.3s ease;
      }
      .caret-down::before {
        transform: rotate(90deg);
      }
      .nested {
        margin-left: 1em;
      }
    </style>
    {% if tree_html %}
      {{ tree_html|safe }}
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
    </script>
    """
    return render_template_string(template, snapshot_id=snapshot_id, tree_html=tree_html)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
