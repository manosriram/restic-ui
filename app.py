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
    template = """
    <h1>Snapshot {{ snapshot_id }} Contents</h1>
    {% if contents %}
    <ul>
    {% for item in contents %}
      <li>{{ item }}</li>
    {% endfor %}
    </ul>
    {% else %}
    <p>No contents found or error retrieving snapshot.</p>
    {% endif %}
    <p><a href="/">Back to snapshots</a></p>
    """
    return render_template_string(template, snapshot_id=snapshot_id, contents=contents)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
