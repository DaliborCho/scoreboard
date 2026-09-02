"""A stand-in customer API, for exercising the REST connector by hand.

    docker compose exec api python /app/tests/fixtures/fake_api.py
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PEOPLE = [
    {"id": "E-100", "full_name": "Nora Bianchi", "crew": "Roof Crew A", "office": "Northgate",
     "leads_issued": 61, "leads_pitched": 48, "deals": 19, "gross": "218,400", "pending": "12,000", "net": 194500},
    {"id": "E-101", "full_name": "Owen Fitzgerald", "crew": "Roof Crew A", "office": "Northgate",
     "leads_issued": 44, "leads_pitched": 30, "deals": 11, "gross": "129,900", "pending": "0", "net": 121300},
    {"id": "E-102", "full_name": "Selma Aydin", "crew": "Roof Crew B", "office": "Northgate",
     "leads_issued": 52, "leads_pitched": 41, "deals": 16, "gross": "176,250", "pending": "8,400", "net": 158900},
    {"id": "E-103", "full_name": "Hugo Marchetti", "crew": "Roof Crew B", "office": "Northgate",
     "leads_issued": 28, "leads_pitched": 17, "deals": 4, "gross": "41,000", "pending": "2,500", "net": 36100},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "data": {"reps": PEOPLE}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("fake customer API on :9099")
    HTTPServer(("0.0.0.0", 9099), Handler).serve_forever()
