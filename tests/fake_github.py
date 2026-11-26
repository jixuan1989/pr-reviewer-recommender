# SPDX-License-Identifier: Apache-2.0
"""An in-process stand-in for the GitHub REST + GraphQL API, used by the tests.

It speaks just enough of the API for the recommender, records every request
(method, path, JSON body — never headers) and lets a test script failures.
The real `gh` CLI talks to it through a shim (see `fake_gh`), so what gets
exercised is the CLI's own field serialization, pagination and error output.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

def _error(status: int, message: str) -> dict:
    """Body of a GitHub error answer."""
    return {"message": message, "documentation_url": "https://docs.github.com/rest",
            "status": str(status)}


NOT_AN_ARRAY = 'For \'properties/reviewers\', "%s" is not an array.'
NOT_COLLABORATOR = ("Reviews may only be requested from collaborators. One or more of the users "
                      "or teams you specified is not a collaborator ")   # GitHub's wording, space included
READ_ONLY = "Resource not accessible by integration"

ROUTES = [
    ("GET", r"^/user$", "user"),
    ("GET", r"^/repos/[^/]+/[^/]+/pulls$", "open_prs"),
    ("GET", r"^/repos/[^/]+/[^/]+/pulls/(\d+)/files$", "pull_files"),
    ("GET", r"^/repos/[^/]+/[^/]+/pulls/(\d+)$", "pull"),
    ("POST", r"^/repos/[^/]+/[^/]+/pulls/(\d+)/requested_reviewers$", "request_reviewers"),
    ("GET", r"^/repos/[^/]+/[^/]+/commits$", "commits"),
    ("GET", r"^/repos/[^/]+/[^/]+/issues/(\d+)/comments$", "comments"),
    ("POST", r"^/repos/[^/]+/[^/]+/issues/(\d+)/comments$", "new_comment"),
    ("PATCH", r"^/repos/[^/]+/[^/]+/issues/comments/(\d+)$", "edit_comment"),
    ("POST", r"^/graphql$", "graphql"),
]


class FakeGitHub:
    """Scriptable state + request log. Start with `start()`, stop with `stop()`."""

    def __init__(self) -> None:
        # pull request under review
        self.pull: dict = {}
        self.pull_files: list[str] = []
        # default-branch commits: {"login": str ("" when not linked), "email": str, "date": iso}
        self.commits: list[dict] = []
        # commits reachable from one ref, keyed by what the `sha` parameter asks
        # for; any other ref answers 404, as it does on GitHub
        self.commits_by_ref: dict[str, list[dict]] = {}
        # merged PRs, GraphQL node shape
        self.merged: list[dict] = []
        self.merged_files: dict[int, list[str]] = {}   # for PRs with more than 100 files
        self.open_prs: list[dict] = []
        self.comments: list[dict] = []
        self.next_comment_id = 100
        self.token_login = "github-actions"
        # failure scripts
        self.reject_reviewers: dict[str, int] = {}      # login -> HTTP status
        self.comment_status = 200
        self.fail_queue: list[tuple[str, int, int]] = []   # (path substring, status, times)
        self.requests: list[tuple[str, str, object]] = []  # (method, path, parsed body | None)

    # ---------- lifecycle ----------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                fake.requests.append((self.command, self.path, body))
                status, payload, link = fake.route(self.command, self.path, body)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if link:
                    self.send_header("Link", link)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _handle

            def log_message(self, *args):  # keep the test output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.base

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ---------- request log helpers ----------
    def calls(self, method: str, part: str = "") -> list[tuple[str, object]]:
        """(path, body) of the requests with this method whose path contains `part`."""
        return [(path, body) for m, path, body in self.requests if m == method and part in path]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, path) for m, path, _ in self.requests if m != "GET"]

    # ---------- routing ----------
    def route(self, method: str, path: str, body) -> tuple[int, object, str]:
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        for verb, pattern, name in ROUTES:
            m = re.match(pattern, parsed.path)
            if m and verb == method:
                queued = self._pop_failure(parsed.path)
                if queued:
                    return queued, _error(queued, f"injected {queued}"), ""
                return getattr(self, "_" + name)(m, parsed, query, body or {})
        return 404, _error(404, "Not Found"), ""

    def _pop_failure(self, path: str) -> int | None:
        for i, (part, status, times) in enumerate(self.fail_queue):
            if part in path and times > 0:
                if times == 1:
                    self.fail_queue.pop(i)
                else:
                    self.fail_queue[i] = (part, status, times - 1)
                return status
        return None

    def _page(self, items: list, parsed, query) -> tuple[list, str]:
        """One page of `items` plus the `Link: rel="next"` header gh follows."""
        per_page = int((query.get("per_page") or ["30"])[0])
        page = int((query.get("page") or ["1"])[0])
        chunk = items[(page - 1) * per_page: page * per_page]
        if page * per_page < len(items):
            rest = {k: v[0] for k, v in query.items() if k != "page"}
            rest["page"] = str(page + 1)
            return chunk, f'<{self.base}{parsed.path}?{urlencode(rest)}>; rel="next"'
        return chunk, ""

    def _user(self, m, parsed, query, body):
        return 200, {"login": self.token_login}, ""

    def _pull(self, m, parsed, query, body):
        return 200, {
            "number": self.pull["number"], "user": {"login": self.pull["user"],
                                                     "type": self.pull.get("user_type")},
            "created_at": self.pull["created_at"], "changed_files": self.pull["changed_files"],
            "requested_reviewers": [{"login": r} for r in self.pull["requested_reviewers"]],
        }, ""

    def _pull_files(self, m, parsed, query, body):
        number = int(m.group(1))
        mine = self.pull.get("number", number)   # no PR configured: the caller's is "the" PR
        paths = self.merged_files.get(number, self.pull_files if number == mine else [])
        chunk, link = self._page([{"filename": p} for p in paths], parsed, query)
        return 200, chunk, link

    def _open_prs(self, m, parsed, query, body):
        chunk, link = self._page(self.open_prs, parsed, query)
        return 200, chunk, link

    def _request_reviewers(self, m, parsed, query, body):
        reviewers = body.get("reviewers")
        if not isinstance(reviewers, list):       # what GitHub answers a string
            return 422, _error(422, NOT_AN_ARRAY % reviewers), ""
        for login in reviewers:
            if status := self.reject_reviewers.get(login):
                message = READ_ONLY if status == 403 else NOT_COLLABORATOR
                return status, _error(status, message), ""
        self.pull.setdefault("requested_reviewers", []).extend(reviewers)
        return 200, {"requested_reviewers": [{"login": r} for r in self.pull["requested_reviewers"]]}, ""

    def _commits(self, m, parsed, query, body):
        if query.get("sha"):
            ref = query["sha"][0]
            if ref not in self.commits_by_ref:
                return 404, _error(404, "Not Found"), ""
            items = self.commits_by_ref[ref]
        else:
            items = self.commits          # no `sha`: the default branch
        if query.get("since"):
            items = [c for c in items if c["date"] >= query["since"][0]]
        if query.get("until"):
            items = [c for c in items if c["date"] <= query["until"][0]]
        chunk, link = self._page(
            [{"author": {"login": c["login"], "type": c.get("type", "User")} if c["login"] else None,
              "commit": {"author": {"email": c["email"], "name": c.get("name", c["email"])},
                         "committer": {"date": c["date"]}}}
             for c in items], parsed, query)
        return 200, chunk, link

    def _comments(self, m, parsed, query, body):
        chunk, link = self._page(
            [{"id": c["id"], "user": {"login": c["user"]}, "body": c["body"]} for c in self.comments],
            parsed, query)
        return 200, chunk, link

    def _new_comment(self, m, parsed, query, body):
        if self.comment_status != 200:
            return self.comment_status, _error(self.comment_status, READ_ONLY), ""
        comment = {"id": self.next_comment_id, "user": self.token_login, "body": body["body"]}
        self.next_comment_id += 1
        self.comments.append(comment)
        return 201, {"id": comment["id"]}, ""

    def _edit_comment(self, m, parsed, query, body):
        if self.comment_status != 200:
            return self.comment_status, _error(self.comment_status, READ_ONLY), ""
        for c in self.comments:
            if c["id"] == int(m.group(1)):
                c["body"] = body["body"]
        return 200, {"id": int(m.group(1))}, ""

    def _graphql(self, m, parsed, query, body):
        start = int(body.get("cursor") or 0)
        pull_requests = {
            "pageInfo": {"endCursor": str(start + 50), "hasNextPage": start + 50 < len(self.merged)},
            "nodes": self.merged[start: start + 50],     # 50 per page, as the query asks
        }
        return 200, {"data": {"repository": {"pullRequests": pull_requests}}}, ""


def merged_pr(number: int, created: str, author: str, files: list[str],
              reviews: list[tuple[str, str, str]] = (), updated: str | None = None) -> dict:
    """Build one GraphQL pull-request node. `reviews` = (login, state, submittedAt)."""
    return {
        "number": number, "title": f"merged #{number}", "author": {"login": author},
        "createdAt": created, "updatedAt": updated or created, "changedFiles": len(files),
        "files": {"nodes": [{"path": f} for f in files[:100]]},
        "reviews": {"nodes": [{"author": {"login": who}, "state": state, "submittedAt": at}
                              for who, state, at in reviews]},
    }
