import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO


MODULE = Path(__file__).parents[1] / "collect.py"


def load_module():
    if not MODULE.exists():
        raise AssertionError("The practice-test collector has not been implemented")
    spec = importlib.util.spec_from_file_location("school_collect", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fragment(qid="10", feedback=False, token="csrf-one", attempt="100"):
    return f'''<input name="question_id" value="{qid}">
    <input name="test_exam_id" value="{attempt}"><input name="_token" value="{token}">
    <div class="question_div"><p>Which &amp; why?</p><img src="/diagram.png"></div>
    <div class="custom-radio option_1"><input name="answer" value="A" checked>
      <label>Wrong <b>choice</b></label></div>
    <div class="custom-radio option_2">{'<img src="/assets/front/img/wbp/right.webp">' if feedback else ''}
      <input name="answer" value="B"><label>Correct choice</label></div>
    {('<div class="explanation"><p>Explanation:</p><p>Because &amp; therefore.</p>'
       '<a onclick="showVideoExplanation(12345)">Watch</a></div>') if feedback else ''}'''


TOPIC = {
    "id": "topic-one", "subject": "Law & Business", "title": "Business Organization",
    "details_url": "https://www.contractorsischool.com/customer/practice-tests/exam-details?tital_id=topic-one&cat_id=law",
}
ROOT = "https://www.contractorsischool.com/customer/practice-tests"


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_feedback_uses_correct_icon_instead_of_checked_wrong_choice(self):
        page = self.m.parse_question(fragment(feedback=True), feedback=True)
        self.assertEqual(page["question"]["correct_answer"], "B")
        self.assertEqual(page["question"]["question"], "Which & why?")
        self.assertEqual(page["question"]["options"][0]["text"], "Wrong choice")
        self.assertIn("Because & therefore.", page["question"]["explanation"])
        self.assertEqual(page["question"]["video_urls"], ["https://player.vimeo.com/video/12345"])
        self.assertEqual(page["question"]["image_urls"], ["https://www.contractorsischool.com/diagram.png"])

    def test_nonempty_terminal_html_is_not_another_question(self):
        self.assertIsNone(self.m.parse_question('<style>p{}</style><a href="/customer/practice-tests/end-exam/x">End Test</a>'))

    def test_malformed_question_or_missing_correct_marker_is_rejected(self):
        for content, feedback in [(fragment(), True), (fragment().replace('name="question_id"', 'name="missing"'), False)]:
            with self.subTest(feedback=feedback):
                with self.assertRaises(ValueError):
                    self.m.parse_question(content, feedback=feedback)

    def test_subject_and_topic_discovery_keeps_the_sites_tital_id_spelling(self):
        subjects = self.m.subject_links('''<a href="/customer/practice-tests/exams?p=law&q=x">LAW | Law &amp; Business</a>
        <a href="/customer/practice-tests/exams?p=electric&q=y">C-10 - Electrical Contractor</a>
        <a href="https://other.invalid/customer/practice-tests/exams?p=evil">C-10</a>''')
        self.assertEqual([s["subject"] for s in subjects], ["Law & Business", "C-10 Electrical"])
        topics = self.m.topic_links('<a href="/customer/practice-tests/exam-details?tital_id=abc&cat_id=law">Safety</a>', "Law & Business")
        self.assertEqual(topics[0]["id"], "abc")
        self.assertEqual(topics[0]["title"], "Safety")

    def test_export_deduplicates_enriches_and_does_not_leak_form_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.m.Store(Path(directory))
            store.add(self.m.parse_question(fragment())["question"], TOPIC)
            store.add(self.m.parse_question(fragment(feedback=True), feedback=True)["question"], TOPIC)
            store.save()
            saved = (Path(directory) / "questions.json").read_text(encoding="utf-8")
            self.assertNotIn("csrf-one", saved)
            self.assertNotIn("test_exam_id", saved)
            parsed = json.loads(saved)
            self.assertEqual(len(parsed["questions"]), 1)
            self.assertEqual(parsed["questions"][0]["correct_answer"], "B")
            restored = self.m.Store(Path(directory))
            self.assertEqual(len(restored.data["questions"]), 1)
            with (Path(directory) / "questions.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["answer_B"], "Correct choice")
            self.assertEqual(rows[0]["correct_answer"], "B")

    def test_conflicting_answers_are_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.m.Store(Path(directory))
            q = self.m.parse_question(fragment(feedback=True), feedback=True)["question"]
            store.add(q, TOPIC)
            with self.assertRaises(ValueError):
                store.add(dict(q, correct_answer="A"), TOPIC)

    def test_csv_formula_text_is_safe_without_changing_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.m.Store(Path(directory))
            q = self.m.parse_question(fragment(feedback=True), feedback=True)["question"]
            q["question"] = '=HYPERLINK("bad")'
            store.add(q, TOPIC)
            store.save()
            with (Path(directory) / "questions.csv").open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["question"], '\'=HYPERLINK("bad")')
            self.assertEqual(store.data["questions"][0]["question"], '=HYPERLINK("bad")')

    def test_bad_http_responses_and_login_redirects_fail_closed(self):
        for status, url, body in [(403, ROOT, "Just a moment..."), (200, "https://www.contractorsischool.com/login", "Sign in"), (200, "https://www.contractorsischool.com/contractors-license-practice-exams", "Public page")]:
            with self.subTest(url=url, status=status):
                with self.assertRaises(ValueError):
                    self.m.validate_response(status, url, body)

    def test_collector_refreshes_form_fields_and_stops_at_terminal_link(self):
        m = self.m

        class Site:
            def __init__(self):
                self.submissions = 0

            def request(self, method, url, data=None):
                if method == "GET":
                    return '<span id="totalQuestionCount">2</span>' + fragment()
                self.submissions += 1
                expected = {"question_id": "10" if self.submissions == 1 else "20", "test_exam_id": "100" if self.submissions == 1 else "101", "_token": "csrf-one" if self.submissions == 1 else "csrf-two", "answer": "A"}
                if data != expected:
                    raise AssertionError(f"Incorrect submission fields: {list(data)}")
                next_html = fragment("20", token="csrf-two", attempt="101") if self.submissions == 1 else '<a href="/customer/practice-tests/end-exam/topic-one">End Test</a>'
                return json.dumps({"status": "success", "data": {"html": fragment(data["question_id"], feedback=True), "next_question_html": next_html, "total_questions": 2, "current_question_count": self.submissions + 1}})

        with tempfile.TemporaryDirectory() as directory:
            store = m.Store(Path(directory))
            site = Site()
            m.collect_topic(site, store, TOPIC, max_questions=3)
            self.assertEqual(len(store.data["questions"]), 2)
            self.assertEqual(store.data["topics"][0]["status"], "complete")
            self.assertEqual(site.submissions, 2)

    def test_question_limit_preserves_partial_results_without_claiming_complete(self):
        class Site:
            def request(self, method, url, data=None):
                if method == "GET":
                    return fragment()
                return json.dumps({"status": "success", "data": {"html": fragment(feedback=True), "next_question_html": fragment("20"), "total_questions": 2}})

        with tempfile.TemporaryDirectory() as directory:
            store = self.m.Store(Path(directory))
            with self.assertRaises(ValueError):
                self.m.collect_topic(Site(), store, TOPIC, max_questions=1)
            saved = json.loads((Path(directory) / "questions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["questions"]), 1)
            self.assertNotEqual(saved["topics"][0]["status"], "complete")

    def test_real_mitmproxy_capture_round_trip_is_idempotent(self):
        from mitmproxy import connection, http, io

        def flow(method, url, body, data=None):
            value = http.HTTPFlow(connection.Client(peername=("127.0.0.1", 1234), sockname=("127.0.0.1", 8080)), connection.Server(address=("www.contractorsischool.com", 443)))
            value.request = http.Request.make(method, url, data or b"", {"Content-Type": "application/x-www-form-urlencoded"})
            value.response = http.Response.make(200, body.encode(), {"Content-Type": "text/html" if method == "GET" else "application/json"})
            return value

        root_html = '<a href="/customer/practice-tests/exams?p=law&q=x">LAW | Law &amp; Business</a>'
        topic_html = '<a href="/customer/practice-tests/exam-details?tital_id=topic-one&cat_id=law">Business Organization</a>'
        end = '<a href="/customer/practice-tests/end-exam/topic-one">End Test</a>'
        from urllib.parse import urlencode

        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture-without-extension"
            with capture.open("wb") as stream:
                writer = io.FlowWriter(stream)
                for item in [flow("GET", ROOT, root_html), flow("GET", ROOT + "/exams?p=law&q=x", topic_html), flow("GET", ROOT + "/start-exam?tital_id=topic-one", fragment()), flow("POST", ROOT + "/submit-exam-question/topic-one", json.dumps({"status": "success", "data": {"html": fragment(feedback=True), "next_question_html": end, "total_questions": 1}}), urlencode({"question_id": "10", "_token": "csrf-one", "test_exam_id": "100", "answer": "A"}))]:
                    writer.add(item)
            store = self.m.Store(Path(directory) / "exports")
            self.assertEqual(self.m.import_capture(capture, store), 1)
            self.assertEqual(self.m.import_capture(capture, store), 1)
            restored = self.m.Store(Path(directory) / "exports")
            self.assertEqual(len(restored.data["questions"]), 1)
            self.assertEqual(restored.data["topics"][0]["status"], "complete")
            self.assertEqual(restored.data["coverage"][1]["questions"], 0)

    def test_resume_skips_finished_law_topic_and_collects_electrical(self):
        class Site:
            def request(self, method, url, data=None):
                if url == ROOT:
                    return '<a href="/customer/practice-tests/exams?p=law&q=x">Law &amp; Business</a><a href="/customer/practice-tests/exams?p=electric&q=y">C-10 Electrical</a>'
                if url == ROOT + "/exams?p=law&q=x":
                    return '<a href="/customer/practice-tests/exam-details?tital_id=topic-one&cat_id=law">Business Organization</a>'
                if url == ROOT + "/exams?p=electric&q=y":
                    return '<a href="/customer/practice-tests/exam-details?tital_id=topic-two&cat_id=electric">Wiring</a>'
                if method == "GET" and url == ROOT + "/start-exam?tital_id=topic-two":
                    return fragment("20")
                if method == "POST" and url == ROOT + "/submit-exam-question/topic-two" and data["question_id"] == "20":
                    return json.dumps({"status": "success", "data": {"html": fragment("20", feedback=True), "next_question_html": '<a href="/customer/practice-tests/end-exam/topic-two">End Test</a>', "total_questions": 1}})
                raise AssertionError("Unexpected request: " + method + " " + url)

        with tempfile.TemporaryDirectory() as directory:
            store = self.m.Store(Path(directory))
            store.add(self.m.parse_question(fragment(feedback=True), feedback=True)["question"], TOPIC)
            store.topic(TOPIC)["status"] = "complete"
            store.save()
            self.assertEqual(self.m.collect_all(Site(), store, 3), 0)
            self.assertEqual([c["questions"] for c in store.data["coverage"]], [1, 1])
            self.assertTrue(all(t["status"] == "complete" for t in store.data["topics"]))

    def test_network_errors_do_not_expose_transport_details(self):
        class Session:
            def request(self, *args, **kwargs):
                raise RuntimeError("Cookie: secret-session-value")

        transport = self.m.HttpTransport.__new__(self.m.HttpTransport)
        transport.session = Session()
        transport.delay = 0
        with self.assertRaises(ValueError) as raised:
            transport.request("GET", ROOT)
        self.assertNotIn("secret-session-value", str(raised.exception))

    def test_corrupt_checkpoint_is_reported_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "questions.json"
            checkpoint.write_text('{"questions":', encoding="utf-8")
            with redirect_stderr(StringIO()):
                result = self.m.main(["collect", "--cdp", "http://127.0.0.1:9333", "--output", directory])
            self.assertEqual(result, 2)
            self.assertEqual(checkpoint.read_text(encoding="utf-8"), '{"questions":')


if __name__ == "__main__":
    unittest.main()
