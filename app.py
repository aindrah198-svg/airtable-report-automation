from pathlib import Path
from flask import Flask, jsonify, request, render_template, send_file
from airtable_client import Settings, ReportError
from jobs import JobStore

ROOT = Path(__file__).resolve().parent


def create_app(store=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    store = store or JobStore(ROOT / ".data/runs")
    app.extensions["jobs"] = store

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        return jsonify(
            configured=Settings.from_env().configured(), jobs=store.history()
        )

    @app.post("/api/runs")
    def start():
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Use the local dashboard to start a run."), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("source") not in (
            "sample",
            "airtable",
        ):
            return jsonify(error="Choose sample or airtable source."), 422
        if body["source"] == "airtable" and not Settings.from_env().configured():
            return (
                jsonify(
                    error="Airtable is not configured on this server. Try sample mode."
                ),
                422,
            )
        try:
            return jsonify(id=store.submit(body["source"])), 202
        except ReportError as error:
            return jsonify(error=str(error)), 409

    @app.get("/api/runs/<job_id>/download")
    def download(job_id):
        job = store.get(job_id)
        if not job:
            return jsonify(error="Run not found."), 404
        if job["status"] != "completed":
            return jsonify(error="No completed report for this run."), 409
        file = store.path / (job_id + ".zip")
        if not file.exists():
            return (
                jsonify(error="This local report archive is missing. Start a new run."),
                404,
            )
        return send_file(
            file,
            as_attachment=True,
            download_name="airtable-report-" + job_id[:8] + ".zip",
        )

    @app.errorhandler(413)
    def large(error):
        return jsonify(error="Request is too large."), 413

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8107, debug=False)
