from flask import Flask

from restic import ResticUI

app = Flask(__name__)

@app.route("/")
def default_route():
    return ResticUI().get_snapshots()
    #  return "Hello, World!"

if __name__ == "__main__":
    app.run(host="100.69.69.69")
