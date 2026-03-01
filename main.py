from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})
