import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session() -> requests.Session:
    retry = Retry(total=3, backoff_factor=0.5, method_whitelist=["GET", "POST"])
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "demo"})
    return session


def fetch_json(url: str) -> dict:
    resp = make_session().get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()
