from flask import Flask, jsonify, request

from .http import fetch_json
from .models import from_payload, to_payload

app = Flask(__name__)


@app.post("/users")
def create_user():
    user = from_payload(request.get_json())
    return jsonify(to_payload(user))


@app.get("/proxy")
def proxy():
    return jsonify(fetch_json(request.args["url"]))
