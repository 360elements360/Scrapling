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

This opens or resumes each practice-test attempt, then ends it to read the full result review. The review exposes correct answers for answered and unanswered questions, so live collection does not submit answer choices. **It changes practice-attempt history and scores, and it ends any unfinished attempt for a collected topic.** An attempt with no answers has a zero score. It does not submit feedback forms or purchase anything. Video and image references are saved; media files are not downloaded.

An HTTP-only alternative uses cookies from a current capture:

```powershell
python -X utf8 tools/contractors_school/collect.py collect --capture 'G:\flows (12)'
```

The supplied capture's HTTP session did not provide live access during verification. Do not assume another run will bypass Cloudflare.

## Resume and coverage

Run the same command with the same output directory to resume. Completed topics are skipped. Interrupted topics open or resume the site's attempt and merge already collected questions by subject, topic and question ID. The JSON checkpoint is saved after each fully validated topic review; CSV is regenerated from it. Partial questions from imported captures are also preserved. Failed requests are not automatically retried. Exit code `2` means collection is incomplete; existing exports are preserved.

Coverage is explicit for both subjects. A complete live topic means all unique question cards and correct answers from its review match the counts declared by both the topic-details page and the result page. Capture import can also mark a full observed attempt complete. This does **not** prove there are no unseen questions in a randomized question bank. Questions seen without answer feedback in a capture remain in JSON with `correct_answer: null`.

Defaults: one second between requests, at most five hundred declared questions per topic. Adjust with `--delay` and `--max-questions`. An over-limit topic stops before starting an attempt. Duplicate question IDs, missing answer markers, conflicting correct answers, mismatched review counts, login redirects or challenges leave the topic incomplete and stop collection.

## Verify

```powershell
python -X utf8 -m unittest discover -s tools/contractors_school/tests -v
```

The suite covers extraction, answer correctness, image/video references, terminal HTML, malformed feedback, CSV safety, token exclusion, checkpoint limits, real mitmproxy file import, resuming across the two subjects and recovering previously answered questions from full reviews. Browser-backed fetching and full-review extraction have also been verified against a signed-in session.

## Reading, audio and resources

With the same signed-in browser open:

```powershell
python -X utf8 tools/contractors_school/materials.py --cdp http://127.0.0.1:9333
```

The collector discovers all available Reading Course, Audio Course and Resources entries. It includes the shared Math & Safety Review alongside Law & Business and C10 Electrical Contractor. It saves content under `exports/contractors-school/materials/`:

- `index.html`: a local index linking to reading books, audio playlists, PDF resources and the question exports.
- `reading/`: individual lessons and combined course books as HTML and text. Diagrams are downloaded under `assets/` and image links rewritten for offline use.
- `audio/`: actual MP3 files, a local track listing and a playlist per course for an audio player.
- `resources/`: the PDF files linked by the site.
- `manifest.json`: source URLs, status, sizes, SHA-256 hashes, audio durations and PDF page counts.

Run the same command again to resume. Existing files must match their recorded hash and size before being skipped. Reading extraction removes account navigation, forms, scripts and event handlers. It reads only the three course sections, preserving the practice-test tool's original request scope by default.

Authenticated HTML is fetched inside Chrome. Public S3 audio and images are downloaded directly with no browser cookies and without inheriting proxy environment settings. Resource PDFs rejected or timed out by ordinary HTTP clients can use Chrome's `Network.loadNetworkResource` and streamed `IO.read`; external resources do not receive the school session's cookies. No proxy is configured by the collector.

Downloads use temporary files and are published only after format validation. The collector checks PDF page counts, playable MP3 duration, image validity and response byte counts. Defaults are four download workers, one second between course-page requests and a 512 MiB limit per file. Adjust with `--workers`, `--delay` and `--max-file-mb`. Transient course-page HTTP 403 responses receive at most three GET attempts with a short delay; other failures remain explicit. A nonzero exit means some materials are incomplete, with verified files preserved for the next run.
