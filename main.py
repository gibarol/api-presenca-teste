from flask import Flask, jsonify
import requests

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-presenca-teste"})

@app.route("/ip")
def ip():
    response = requests.get("https://api.ipify.org?format=json")
    return jsonify(response.json())
