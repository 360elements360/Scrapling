"""Export captured or authenticated Contractors iSchool practice-test questions."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import urlopen

from bs4 import BeautifulSoup


ORIGIN = "https://www.contractorsischool.com"
ROOT = ORIGIN + "/customer/practice-tests"
SUBJECTS = ["Law & Business", "C-10 Electrical"]


def soup(text):
    return BeautifulSoup(text, "html.parser")


def text_of(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def safe_url(url, allowed_paths=None):
    absolute = urljoin(ORIGIN, url)
    parts = urlsplit(absolute)
    if parts.scheme != "https" or parts.netloc != "www.contractorsischool.com":
        raise ValueError("Refusing a request outside the school website")
    paths = allowed_paths or ("/customer/practice-tests",)
    if not any(parts.path == path.rstrip("/") or parts.path.startswith(path.rstrip("/") + "/") for path in paths):
        raise ValueError("Refusing a request outside the permitted course sections")
    return absolute


def subject_links(html):
    links = {}
    for link in soup(html).select("a[href]"):
        try:
            url = safe_url(link["href"])
        except ValueError:
            continue
        if urlsplit(url).path != "/customer/practice-tests/exams":
            continue
        label = text_of(link)
        if re.search(r"C[- ]?10", label, re.I):
            subject = SUBJECTS[1]
        elif "law" in label.lower():
            subject = SUBJECTS[0]
        else:
            continue
        links[subject] = {"subject": subject, "url": url}
    return list(links.values())


def topic_links(html, subject):
    topics = {}
    for link in soup(html).select("a[href]"):
        try:
            url = safe_url(link["href"])
        except ValueError:
            continue
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path != "/customer/practice-tests/exam-details" or "tital_id" not in query:
            continue
        ident = query["tital_id"][0]
        topics[ident] = {"id": ident, "subject": subject, "title": text_of(link), "details_url": url}
    return list(topics.values())


def parse_question(html, feedback=False):
    doc = soup(html)
    question = doc.select_one(".question_div")
    if question is None:
        return None
    form = {n["name"]: n.get("value", "") for n in doc.select("input[name]") if n["name"] in {"question_id", "test_exam_id", "_token"}}
    if not form.get("question_id") or not text_of(question):
        raise ValueError("Question is missing its ID or text")
    options, correct = [], []
    for choice in doc.select('input[name="answer"]'):
        container = choice.find_parent(class_="custom-radio")
        if container is None:
            raise ValueError("Answer choice has no option container")
        label = container.find("label")
        key = choice.get("value", "")
        if not key or label is None:
            raise ValueError("Answer choice is missing its key or label")
        options.append({"key": key, "text": text_of(label), "image_urls": [urljoin(ORIGIN, n["src"]) for n in label.select("img[src]")]})
        if any(urlsplit(n["src"]).path.endswith("/right.webp") for n in container.select("img[src]")):
            correct.append(key)
    if len(options) < 2 or len({o["key"] for o in options}) != len(options):
        raise ValueError("Question has missing or duplicate answer choices")
    if len(correct) > 1 or (feedback and len(correct) != 1):
        raise ValueError("Feedback must identify exactly one correct answer")
    explanation = doc.select_one(".explanation")
    # Extract prose separately from the video button/modal text.
    if explanation:
        for node in explanation.select("script, style, .video_explanation, .modal"):
            node.decompose()
    explanation_text = re.sub(r"^Explanation:\s*", "", text_of(explanation))
    images = []
    for container in [question, explanation] + list(doc.select(".custom-radio label")):
        if container:
            images.extend(urljoin(ORIGIN, n["src"]) for n in container.select("img[src]"))
    result = {
        "question_id": form["question_id"], "question": text_of(question), "options": options,
        "correct_answer": correct[0] if correct else None, "explanation": explanation_text,
        "video_urls": sorted({"https://player.vimeo.com/video/" + ident for ident in re.findall(r"showVideoExplanation\((\d+)\)", html)}),
        "image_urls": sorted(set(images)),
    }
    return {"question": result, "form": form}


def declared_total(html):
    plain = text_of(soup(html))
    match = re.search(r"Total Questions\s+(\d+)|Total Question\s*:\s*(\d+)", plain)
    if match is None:
        raise ValueError("Page does not declare its question count")
    return int(next(value for value in match.groups() if value is not None))


def parse_review_questions(html):
    doc = soup(html)
    questions, seen = [], set()
    for question in doc.select(".question_div"):
        card = question.parent
        while card is not None and len(card.select(".question_div")) == 1 and not card.select('input[type="radio"]'):
            card = card.parent
        if card is None or len(card.select(".question_div")) != 1:
            raise ValueError("Could not isolate a review question card")
        copy = soup(str(card))
        radios = copy.select('input[type="radio"]')
        ids = set()
        for radio in radios:
            match = re.fullmatch(r"answer\d+_(\d+)", radio.get("name", ""))
            if match is None:
                raise ValueError("Review radio name lacks a question ID")
            ids.add(match[1])
            radio["name"] = "answer"
        if len(ids) != 1:
            raise ValueError("Review choices have inconsistent question IDs")
        ident = ids.pop()
        if ident in seen:
            raise ValueError("Duplicate question in review")
        seen.add(ident)
        copy.insert(0, copy.new_tag("input", attrs={"name": "question_id", "value": ident}))
        parsed = parse_question(str(copy), feedback=True)
        if parsed is None:
            raise ValueError("Review card contains no question")
        questions.append(parsed["question"])
    if not questions:
        raise ValueError("Review contains no questions")
    return questions


class Store:
    """The JSON export is the checkpoint; CSV is regenerated from it."""

    def __init__(self, output):
        self.output = Path(output)
        filename = self.output / "questions.json"
        self.data = json.loads(filename.read_text(encoding="utf-8")) if filename.exists() else {
            "schema_version": 1, "requested_subjects": SUBJECTS, "questions": [], "topics": [],
        }
        if self.data.get("schema_version") != 1:
            raise ValueError("Unsupported export schema; use a separate output directory")
        self.index = {(q["subject"], q["topic_id"], q["question_id"]): q for q in self.data["questions"]}

    def topic(self, info):
        existing = next((t for t in self.data["topics"] if (t["subject"], t["id"]) == (info["subject"], info["id"])), None)
        if existing is None:
            existing = dict(info, status="pending")
            self.data["topics"].append(existing)
        else:
            existing.update({k: v for k, v in info.items() if k not in {"status", "error"}})
        return existing

    def add(self, question, topic):
        self.topic(topic)
        key = (topic["subject"], topic["id"], question["question_id"])
        previous = self.index.get(key)
        if previous:
            if previous.get("correct_answer") and question.get("correct_answer") and previous["correct_answer"] != question["correct_answer"]:
                raise ValueError("Conflicting correct answers for question " + question["question_id"])
            for field, value in question.items():
                if value:
                    previous[field] = value
        else:
            record = dict(question, subject=topic["subject"], topic=topic["title"], topic_id=topic["id"], source_url=topic["details_url"])
            self.index[key] = record
            self.data["questions"].append(record)

    def save(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.data["coverage_note"] = "Complete means all declared questions were observed in a captured attempt or full review; unseen randomized questions may exist."
        self.data["coverage"] = []
        for subject in SUBJECTS:
            topics = [t for t in self.data["topics"] if t["subject"] == subject]
            questions = [q for q in self.data["questions"] if q["subject"] == subject]
            self.data["coverage"].append({
                "subject": subject, "topics_discovered": len(topics),
                "topics_complete": sum(t["status"] == "complete" for t in topics),
                "questions": len(questions), "answers_known": sum(bool(q["correct_answer"]) for q in questions),
            })
        self.data["questions"].sort(key=lambda q: (q["subject"], q["topic"], q["question_id"]))
        tmp = self.output / "questions.json.tmp"
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.output / "questions.json")
        fields = ["subject", "topic", "question_id", "question", "answer_A", "answer_B", "answer_C", "answer_D", "correct_answer", "explanation", "video_urls", "image_urls", "options_json", "source_url"]
        tmp = self.output / "questions.csv.tmp"
        with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for q in self.data["questions"]:
                row = {k: q.get(k, "") for k in fields}
                row.update({"answer_" + o["key"]: o["text"] for o in q["options"] if o["key"] in "ABCD"})
                row["video_urls"] = " | ".join(q["video_urls"])
                row["image_urls"] = " | ".join(q["image_urls"])
                row["options_json"] = json.dumps(q["options"], ensure_ascii=False)
                # Spreadsheet formula protection applies only to CSV, preserving exact JSON text.
                row = {k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v) for k, v in row.items()}
                writer.writerow(row)
        tmp.replace(self.output / "questions.csv")


def read_flows(path):
    from mitmproxy import http, io

    with Path(path).open("rb") as stream:
        return [f for f in io.FlowReader(stream).stream() if isinstance(f, http.HTTPFlow) and urlsplit(f.request.pretty_url).hostname == "www.contractorsischool.com"]


def import_capture(path, store):
    flows = read_flows(path)
    categories = {}
    for flow in flows:
        if flow.response and flow.response.status_code == 200 and urlsplit(flow.request.pretty_url).path == "/customer/practice-tests":
            for entry in subject_links(flow.response.get_text(strict=False) or ""):
                categories[parse_qs(urlsplit(entry["url"]).query).get("p", [""])[0]] = entry["subject"]
    for flow in flows:
        if not flow.response or flow.response.status_code != 200:
            continue
        parts = urlsplit(flow.request.pretty_url)
        if parts.path != "/customer/practice-tests/exams":
            continue
        category = parse_qs(parts.query).get("p", [""])[0]
        subject = categories.get(category)
        if subject:
            for topic in topic_links(flow.response.get_text(strict=False) or "", subject):
                store.topic(topic)
    topics = {t["id"]: t for t in store.data["topics"]}
    current_attempt = {}
    imported = 0
    for flow in flows:
        if not flow.response or flow.response.status_code != 200:
            continue
        parts = urlsplit(flow.request.pretty_url)
        if parts.path == "/customer/practice-tests/start-exam":
            ident = parse_qs(parts.query).get("tital_id", [None])[0]
            current_attempt[ident] = set()
            if ident in topics:
                page = parse_question(flow.response.get_text(strict=False) or "")
                if page:
                    store.add(page["question"], topics[ident])
        elif "/customer/practice-tests/submit-exam-question/" in parts.path:
            ident = parts.path.rsplit("/", 1)[-1]
            if ident not in topics:
                raise ValueError("Capture lacks topic navigation for an observed test; capture the subject/topic index too")
            response = json.loads(flow.response.get_text(strict=False))
            if response.get("status") != "success":
                continue
            data = response["data"]
            page = parse_question(data["html"], feedback=True)
            if page is None:
                raise ValueError("Successful submission contains no question")
            posted = dict(flow.request.urlencoded_form)
            if posted.get("question_id") != page["form"]["question_id"]:
                raise ValueError("Captured feedback does not match the submitted question")
            store.add(page["question"], topics[ident])
            current_attempt.setdefault(ident, set()).add(page["form"]["question_id"])
            imported += 1
            next_page = parse_question(data.get("next_question_html", ""))
            if next_page:
                store.add(next_page["question"], topics[ident])
            elif terminal_link(data.get("next_question_html", "")):
                expected = int(data["total_questions"])
                topics[ident]["expected_questions"] = expected
                if len(current_attempt[ident]) == expected:
                    topics[ident]["status"] = "complete"
    if not imported:
        raise ValueError("Capture contains no successful practice-question submissions")
    store.data["capture_source"] = Path(path).name
    store.save()
    return imported


def terminal_link(html):
    for node in soup(html).select("a[href]"):
        try:
            url = safe_url(node["href"])
        except ValueError:
            continue
        if urlsplit(url).path.startswith("/customer/practice-tests/end-exam/"):
            return url
    return None


def validate_response(status, url, body, allowed_paths=None):
    if status != 200:
        raise ValueError(f"Website returned HTTP {status}; use a current signed-in browser session")
    try:
        safe_url(url, allowed_paths)
    except ValueError:
        raise ValueError("Website redirected outside the signed-in practice tests; sign in again") from None
    if re.search(r"<title[^>]*>\s*Just a moment", body, re.I):
        raise ValueError("Cloudflare challenge; open the practice tests in your signed-in browser")
    return body


class HttpTransport:
    def __init__(self, capture, delay):
        from curl_cffi import requests

        flows = read_flows(capture)
        request = next((f.request for f in reversed(flows) if "/cdn-cgi/" not in f.request.path), None)
        if request is None:
            raise ValueError("Capture has no site request to obtain session cookies from")
        agent = request.headers.get("user-agent", "")
        self.session = requests.Session(impersonate="firefox" if "Firefox" in agent else "chrome", trust_env=False, headers={"User-Agent": agent})
        # Populate a cookie jar so Set-Cookie refreshes take effect on subsequent requests.
        from http.cookies import SimpleCookie

        cookies = SimpleCookie()
        cookies.load("; ".join(request.headers.get_all("cookie")))
        for name, morsel in cookies.items():
            self.session.cookies.set(name, morsel.value, domain="www.contractorsischool.com", path="/")
        self.delay = delay

    def request(self, method, url, data=None):
        url = safe_url(url)
        time.sleep(self.delay)
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": ROOT} if method == "POST" else {}
        try:
            response = self.session.request(method, url, data=data, headers=headers, timeout=30, allow_redirects=False)
        except Exception as error:
            raise ValueError("HTTP transport failed (" + type(error).__name__ + "); saved answers are preserved") from None
        return validate_response(response.status_code, response.url, response.text)


class BrowserTransport:
    """Use fetch inside an already-open, authenticated school tab, without copying cookies."""

    def __init__(self, endpoint, delay, allowed_paths=None):
        import websocket

        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Browser debugging endpoint must be on localhost")
        with urlopen(endpoint.rstrip("/") + "/json", timeout=5) as response:
            targets = json.load(response)
        target = next((t for t in targets if t.get("type") == "page" and urlsplit(t.get("url", "")).hostname == "www.contractorsischool.com"), None)
        if target is None:
            raise ValueError("Open the school practice tests in the debug browser first")
        self.socket = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=40, suppress_origin=True)
        self.ident = 0
        self.delay = delay
        self.allowed_paths = allowed_paths

    def call(self, method, params):
        self.ident += 1
        try:
            self.socket.send(json.dumps({"id": self.ident, "method": method, "params": params}))
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.1, deadline - time.monotonic()))
                message = json.loads(self.socket.recv())
                if message.get("id") == self.ident:
                    break
            else:
                raise TimeoutError
        except Exception as error:
            raise ValueError("Browser transport failed (" + type(error).__name__ + "); saved answers are preserved") from None
        if "error" in message:
            raise ValueError("Browser command failed: " + method)
        return message.get("result", {})

    def request(self, method, url, data=None):
        url = safe_url(url, self.allowed_paths)
        time.sleep(self.delay)
        args = json.dumps({"url": url, "method": method, "data": data})
        expression = """(async (args) => {
            const options = {method: args.method, credentials: 'include', redirect: 'error', signal: AbortSignal.timeout(30000)};
            if (args.data) {
                options.headers = {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With':'XMLHttpRequest'};
                options.body = new URLSearchParams(args.data).toString();
            }
            const r = await fetch(args.url, options);
            return {status:r.status, url:r.url, text:await r.text()};
        })(""" + args + ")"
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise ValueError("Browser request failed; check the signed-in practice-test tab")
        response = result.get("result", {}).get("value")
        if not isinstance(response, dict):
            raise ValueError("Browser did not return a usable response")
        return validate_response(response["status"], response["url"], response["text"], self.allowed_paths)


def collect_topic(transport, store, topic, max_questions=500):
    if max_questions < 1:
        raise ValueError("Question limit must be positive")
    progress = store.topic(topic)
    progress["status"] = "in_progress"
    progress.pop("error", None)
    store.save()
    expected = declared_total(transport.request("GET", topic["details_url"]))
    progress["expected_questions"] = expected
    if expected < 1 or expected > max_questions:
        store.save()
        raise ValueError("Topic question count is outside the configured limit; existing answers are preserved")
    start_url = ROOT + "/start-exam?" + urlencode({"tital_id": topic["id"]})
    page = parse_question(transport.request("GET", start_url))
    if page is None:
        raise ValueError("Start page contains no question; session or access may have expired")
    # Starting can resume an older attempt. Its review includes both earlier answers
    # and unanswered questions, so completeness must be checked against the review.
    review = transport.request("GET", ROOT + "/end-exam/" + topic["id"])
    questions = parse_review_questions(review)
    if declared_total(review) != expected or len(questions) != expected:
        store.save()
        raise ValueError("Review does not match the topic's declared question count")
    for question in questions:
        store.add(question, topic)
    progress["review_questions"] = len(questions)
    progress["collection_method"] = "exam_review"
    progress["status"] = "complete"
    store.save()


def collect_all(transport, store, max_questions):
    subjects = subject_links(transport.request("GET", ROOT))
    if {s["subject"] for s in subjects} != set(SUBJECTS):
        raise ValueError("Signed-in index does not expose both requested subjects")
    for entry in subjects:
        topics = topic_links(transport.request("GET", entry["url"]), entry["subject"])
        if not topics:
            raise ValueError("No topics found for " + entry["subject"])
        for topic in topics:
            store.topic(topic)
    store.save()
    failures = 0
    for topic in store.data["topics"]:
        if topic["status"] == "complete":
            continue
        print("Collecting: " + topic["subject"] + " / " + topic["title"], flush=True)
        try:
            collect_topic(transport, store, topic, max_questions)
        except (ValueError, KeyError, TypeError) as error:
            topic["status"] = "incomplete"
            topic["error"] = str(error)
            store.save()
            failures += 1
            print("Stopped topic: " + str(error), file=sys.stderr, flush=True)
            # An auth/block response must not trigger retries across every remaining topic.
            break
        print("Saved: " + str(topic["review_questions"]) + " reviewed questions", flush=True)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["import", "collect"])
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--output", type=Path, default=Path("exports/contractors-school"))
    parser.add_argument("--cdp", help="Existing localhost Chrome debug endpoint with a signed-in school tab")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-questions", type=int, default=500)
    args = parser.parse_args(argv)
    if args.delay < 0 or args.max_questions < 1:
        parser.error("delay must be nonnegative and max-questions must be positive")
    if args.mode == "import" and not args.capture:
        parser.error("import requires --capture")
    if args.mode == "collect" and not (args.cdp or args.capture):
        parser.error("collect requires --cdp or --capture for authentication")
    try:
        store = Store(args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Cannot open checkpoint; existing file was not changed (" + type(error).__name__ + ")", file=sys.stderr)
        return 2
    try:
        if args.capture:
            count = import_capture(args.capture, store)
            print(f"Imported {count} answer responses; {len(store.data['questions'])} unique question records", flush=True)
        if args.mode == "collect":
            transport = BrowserTransport(args.cdp, args.delay) if args.cdp else HttpTransport(args.capture, args.delay)
            if collect_all(transport, store, args.max_questions):
                return 2
        store.save()
        print(json.dumps(store.data["coverage"], ensure_ascii=False), flush=True)
        print("Exports: " + str(args.output.resolve()), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        # Do not print request bodies, cookie headers, or exception objects from HTTP libraries.
        store.save()
        print("Collection incomplete: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
