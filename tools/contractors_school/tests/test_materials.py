import base64
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).parents[1]
sys.path.insert(0, str(TOOL))
import collect

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
ASSET = 'https://contractorsischool.s3.us-west-1.amazonaws.com/image.png'


class Response:
    def __init__(self, body=PNG, size=None, status=200):
        self.status_code = status
        self.headers = {'content-length': str(len(body) if size is None else size), 'content-type': 'image/png'}
        self.body = body
        self.url = ASSET

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


class Session:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response

    def close(self):
        pass


class MaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filename = TOOL / 'materials.py'
        if not filename.exists():
            raise AssertionError('Course-material collector not implemented')
        spec = importlib.util.spec_from_file_location('materials', filename)
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        spec.loader.exec_module(cls.m)

    def test_material_scope_keeps_account_actions_and_similar_prefixes_blocked(self):
        self.assertEqual(collect.safe_url('/customer/reading-courses/10', self.m.COURSE_PATHS), collect.ORIGIN + '/customer/reading-courses/10')
        for url in ['/customer/my-account', '/customer/reading-courses-evil', 'https://other.invalid/customer/reading-courses']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                collect.safe_url(url, self.m.COURSE_PATHS)

    def test_lesson_discovery_deduplicates_parent_links_and_keeps_leaf_labels(self):
        html = '''<a href="/customer/reading-courses/details/564?category=43&chapter=70">Health &amp; Safety</a>
        <a href="/customer/reading-courses/details/564?category=43&chapter=70&sub_chapter=564">Scaffolding</a>
        <a href="/customer/reading-courses/details/565">Frame Scaffold</a>
        <a href="/customer/reading-courses/details/564">Continue Reading</a>'''
        items = self.m.lesson_links(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['id'], '564')
        self.assertEqual(items[0]['title'], 'Scaffolding')
        self.assertIn('sub_chapter=564', items[0]['url'])

    def test_reading_extraction_preserves_tables_and_images_without_account_markup(self):
        html = f'''<nav>Private account</nav><div class="tabContent-area">
        <div class="reading-title"><h5>Wire Types</h5></div><p>Use <b>copper</b>.</p>
        <table><tr><td>Gauge</td><td>Size</td></tr></table><img src="{ASSET}" onerror="bad()">
        <script>secret()</script><form><input value="private-csrf"></form>
        <div class="row"><a href="/customer/reading-courses/details/1">Prev</a><a href="/customer/reading-courses/details/3">Next</a></div></div>'''
        item = self.m.reading_content(html)
        self.assertEqual(item['title'], 'Wire Types')
        self.assertEqual(item['images'], [ASSET])
        self.assertIn('<table>', item['html'])
        self.assertIn('copper', item['text'])
        for private in ['Private account', 'private-csrf', 'secret()', 'onerror', '>Next<']:
            self.assertNotIn(private, item['html'])

    def test_audio_discovery_ignores_non_audio_sources(self):
        url = 'https://contractorsischool.s3.us-west-1.amazonaws.com/audio/lesson.mp3'
        self.assertEqual(self.m.audio_source(f'<video><source src="https://other.invalid/ad.mp4"></video><audio><source src="{url}"></audio>'), url)
        with self.assertRaises(ValueError):
            self.m.audio_source('<audio></audio>')

    def test_resource_links_upgrade_http_and_ignore_navigation(self):
        items = self.m.resource_links('<a href="http://www.cslb.ca.gov/Resources/test.pdf">Study guide</a><a href="/customer/my-account">Account</a>')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://www.cslb.ca.gov/Resources/test.pdf')

    def test_download_verifies_real_image_bytes_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'image.png'
            with patch.object(self.m.requests, 'Session', return_value=Session(Response())):
                result = self.m.download_file(ASSET, dest, 'image', max_bytes=1000)
            self.assertEqual(dest.read_bytes(), PNG)
            self.assertEqual(result['sha256'], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(result['bytes'], len(PNG))

    def test_truncated_download_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'image.png'
            dest.write_bytes(b'previous-good-file')
            with patch.object(self.m.requests, 'Session', return_value=Session(Response(size=len(PNG) + 1))):
                with self.assertRaises(ValueError):
                    self.m.download_file(ASSET, dest, 'image', max_bytes=1000)
            self.assertEqual(dest.read_bytes(), b'previous-good-file')

    def test_download_size_guard_rejects_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'image.png'
            with patch.object(self.m.requests, 'Session', return_value=Session(Response(size=5000))):
                with self.assertRaises(ValueError):
                    self.m.download_file(ASSET, dest, 'image', max_bytes=100)
            self.assertFalse(dest.exists())

    def test_browser_fallback_writes_the_actual_returned_binary_stream(self):
        class Browser:
            def download(self, url, path, max_bytes):
                if url != ASSET:
                    raise AssertionError('Wrong download source')
                path.write_bytes(PNG)
                return {'content-length': str(len(PNG)), 'content-type': 'image/png'}

        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'image.png'
            with patch.object(self.m.requests, 'Session', return_value=Session(Response(status=403))):
                result = self.m.download_file(ASSET, dest, 'image', browser=Browser(), max_bytes=1000)
            self.assertEqual(dest.read_bytes(), PNG)
            self.assertEqual(result['bytes'], len(PNG))

    def test_direct_connection_timeout_can_recover_through_browser(self):
        class TimeoutSession(Session):
            def get(self, *args, **kwargs):
                raise self_error

        class Browser:
            def download(self, url, path, max_bytes):
                path.write_bytes(PNG)
                return {'content-length': str(len(PNG))}

        self_error = self.m.requests.exceptions.ConnectTimeout('Provider connection timed out')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'
            with patch.object(self.m.requests, 'Session', return_value=TimeoutSession(None)):
                result = self.m.download_file(ASSET, path, 'image', browser=Browser(), max_bytes=1000)
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(result['transport'], 'browser')

    def test_asset_urls_and_filenames_cannot_escape_download_scope(self):
        for url in ['file:///etc/passwd', 'https://127.0.0.1/test.pdf', 'https://other.invalid/test.pdf']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.m.asset_url(url)
        self.assertNotIn('/', self.m.slug('../../Unsafe: title?'))
        self.assertNotIn('\\', self.m.slug('../../Unsafe: title?'))

    def test_transient_browser_get_403_is_retried(self):
        browser = self.m.MaterialBrowser.__new__(self.m.MaterialBrowser)
        with patch.object(collect.BrowserTransport, 'request', side_effect=[ValueError('Website returned HTTP 403'), '<audio>ready</audio>']), patch('time.sleep'):
            self.assertEqual(browser.request('GET', collect.ORIGIN + '/customer/audio-courses/15/play'), '<audio>ready</audio>')

    def test_browser_get_retry_stops_after_three_failures(self):
        browser = self.m.MaterialBrowser.__new__(self.m.MaterialBrowser)
        failures = [ValueError('Website returned HTTP 403') for _ in range(3)]
        with patch.object(collect.BrowserTransport, 'request', side_effect=failures), patch('time.sleep'):
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                browser.request('GET', collect.ORIGIN + '/customer/audio-courses/15/play')

    def test_resume_checks_hash_instead_of_trusting_file_existence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.mp3').write_bytes(b'changed')
            entry = {'path': 'file.mp3', 'bytes': 7, 'sha256': hashlib.sha256(b'correct').hexdigest(), 'status': 'complete'}
            self.assertFalse(self.m.valid_existing(root, entry))
            (root / 'file.mp3').write_bytes(b'correct')
            self.assertTrue(self.m.valid_existing(root, entry))


if __name__ == '__main__':
    unittest.main()
