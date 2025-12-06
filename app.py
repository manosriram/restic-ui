from flask import Flask

from restic import ResticUI

app = Flask(__name__)

from flask import render_template_string

@app.route("/")
def default_route():
    snapshots = ResticUI().get_snapshots()
    # Simple HTML template to display snapshots in a table
    template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Restic Snapshots</title>
        <style>
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; }
            th { background-color: #f2f2f2; }
        </style>
    </head>
    <body>
        <h1>Restic Snapshots</h1>
        {% if snapshots %}
        <table>
            <thead>
                <tr>
                    <th>Snapshot ID</th>
                    <th>Time</th>
                    <th>Hostname</th>
                    <th>Paths</th>
                </tr>
            </thead>
            <tbody>
            {% for snap in snapshots %}
                <tr>
                    <td>{{ snap.id }}</td>
                    <td>{{ snap.time }}</td>
                    <td>{{ snap.hostname }}</td>
                    <td>{{ snap.paths | join(', ') }}</td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>No snapshots found.</p>
        {% endif %}
    </body>
    </html>
    """
    return render_template_string(template, snapshots=snapshots)

if __name__ == "__main__":
    app.run(host="100.69.69.69")
