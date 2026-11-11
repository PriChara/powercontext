"""Exercise the workflow's functions against a real PowerContext HTTP server."""

import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from game import finale, payload, prepare, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18375")
    args = parser.parse_args()

    def post(path, body):
        request = Request(args.url.rstrip("/") + path, data=body.encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return response.read().decode(), response.status
        except HTTPError as error:
            raise RuntimeError(f"{path}: HTTP {error.code}: {error.read().decode()}") from error

    def open_scope(action, ticket=""):
        run = prepare(action, ticket)
        body, status = post("/v1/scopes", run["scope_body"])
        return run, payload(body, status, run["seal"]), json.loads(body)["scope_id"]

    def deposit(action):
        run, request, scope = open_scope(action)
        result, status = post("/v1/memory/remember", request["remember_body"])
        assert "回执" in receipt(result, status, run["ticket"], run["seal"])["story"]
        print(f"{action}: HTTP {status}, confirmed memory record")
        return run["ticket"], scope

    def play(action, ticket, scope, ending):
        run, request, observed_scope = open_scope(action, ticket)
        if scope is not None:
            assert observed_scope == scope, "returning player should see the same scope"
        result, status = post("/v1/context/prepare", request["recall_body"])
        story = finale(result, status, run["seal"])
        assert story["ending"] == ending, story
        print(f"{action}: HTTP {status}, context={json.loads(result)['status']}, ending={ending}; {story['story']}")

    rabbit_ticket, rabbit_scope = deposit("开局：寄存月兔章")
    ring_ticket, ring_scope = deposit("开局：寄存星环章")
    assert rabbit_scope != ring_scope
    play("回来：走光环门", rabbit_ticket, rabbit_scope, "retry")
    play("回来：走脚印门", rabbit_ticket, rabbit_scope, "open")
    play("回来：走光环门", ring_ticket, ring_scope, "open")
    play("回来：走脚印门", "0" * 32, None, "missing")


if __name__ == "__main__":
    main()
