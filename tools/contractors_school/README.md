# Contractors iSchool question collection

Collects the signed-in **Law & Business** and **C-10 Electrical** practice-test sections into portable JSON and CSV. This is a site-specific tool; it does not modify the Scrapling library.

Run commands from the repository root in PowerShell. The four dependencies in `requirements.txt` are already available in the Python installation used to verify this tool. For another environment:

```powershell
python -m pip install -r tools/contractors_school/requirements.txt
```

## Import a saved capture

```powershell
python -X utf8 tools/contractors_school/collect.py import --capture 'G:\flows (12)'
```

The original capture is read without modification. Extensionless mitmproxy files work. Capture the subject index, topic list, start page and answer submissions together so the importer can assign questions to their subjects and topics.

Outputs under `exports/contractors-school/`:

- `questions.json`: full options, correct answer, explanation, video/image references, source topic and coverage/checkpoint metadata.
- `questions.csv`: spreadsheet-friendly question rows, four answer columns, correct answer, explanation, video/image references and full option JSON.

The exports and `.local/` are excluded from git. Session cookies, CSRF tokens and attempt IDs are never written into exports. CSV formula-like values are escaped for spreadsheets; the JSON preserves the text.

## Continue with a signed-in browser

Cloudflare may reject direct HTTP requests even with valid cookies. The preferred live mode sends requests **inside an existing signed-in Chrome tab**, keeping its cookies in the browser.

If no debug browser is already available, **you can manually run** this command to open an isolated persistent Chrome profile. It opens only a localhost debugging listener; use this profile for the school session. This step was not run successfully by the agent because automatic approval review rejected launching a debug browser.

```powershell
& 'C:\Program Files\Google\Chrome\Application\chrome.exe' --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 --user-data-dir=C:\Users\j\claude_remote_debug 'https://www.contractorsischool.com/customer/practice-tests'
```

Sign in on that tab and open the signed-in Practice Tests index. If the site presents a human-verification prompt, complete it yourself. Then:

```powershell
python -X utf8 tools/contractors_school/collect.py collect --cdp http://127.0.0.1:9333 --capture 'G:\flows (12)'
```

No browser is launched or security settings changed by the collector. Keep the school tab open and avoid taking another practice test in that same session during collection. Close the debug browser when finished.

This creates practice-test attempts and submits the first option to reveal the site's feedback. **It changes practice-attempt history and scores.** It does not submit feedback forms or purchase anything. Video references are saved; video files are not downloaded.

An HTTP-only alternative uses cookies from a current capture:

```powershell
python -X utf8 tools/contractors_school/collect.py collect --capture 'G:\flows (12)'
```

The supplied capture's HTTP session did not provide live access during verification. Do not assume another run will bypass Cloudflare.

## Resume and coverage

Run the same command with the same output directory to resume. Completed topics are skipped. Interrupted topics restart a fresh attempt, merging already collected questions by subject, topic and question ID. The JSON checkpoint is saved after each successful answer; CSV is regenerated from it. A failed request is not automatically retried, avoiding duplicate submissions after ambiguous network failures. Exit code `2` means collection is incomplete; existing exports are preserved.

Coverage is explicit for both subjects. A complete topic means one observed full attempt, with the declared number of questions and a terminal End Test link. It does **not** prove there are no unseen questions in a randomized question bank. Questions seen without answer feedback remain in JSON with `correct_answer: null`.

Defaults: one second between requests, at most five hundred submissions per topic. Adjust with `--delay` and `--max-questions`. A limit, repeated question, missing answer marker, conflicting correct answer, unexpected terminal page, login redirect or challenge leaves the topic incomplete and stops collection.

## Verify

```powershell
python -X utf8 -m unittest discover -s tools/contractors_school/tests -v
```

The suite covers extraction, answer correctness, image/video references, terminal HTML, malformed feedback, CSV safety, token exclusion, checkpoint limits, real mitmproxy file import and resuming across the two subjects. Browser fetching itself still needs verification against a current signed-in debug session.
