"""Download the account's reading, audio and resource materials for offline use."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from collect import BrowserTransport, ORIGIN, safe_url, soup, text_of

COURSE_PATHS = tuple('/customer/' + name for name in ['reading-courses', 'audio-courses', 'reading-resources'])
ASSET_HOSTS = {
    'contractorsischool.s3.us-west-1.amazonaws.com',
    'contractorsischool.s3-us-west-1.amazonaws.com',
    'contractorsischool.com', 'cslb.ca.gov', 'edd.ca.gov', 'dir.ca.gov',
    'brooksolar.com', 'energy-ready.org',
}


def slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    value = re.sub('[^a-z0-9]+', '-', value).strip('-')[:90] or 'item'
    if value.upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}:
        value = 'item-' + value
    return value


def asset_url(url):
    parts = urlsplit(urljoin(ORIGIN, url))
    host = (parts.hostname or '').removeprefix('www.')
    if parts.scheme not in {'http', 'https'} or host not in ASSET_HOSTS or parts.username or parts.password:
        raise ValueError('Asset URL is outside the observed public download hosts')
    if host == 'contractorsischool.com' and not parts.path.startswith(('/assets/', '/uploads/', '/storage/', '/images/')):
        raise ValueError('School download must be a static asset')
    return urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))


def course_links(html, section):
    result = {}
    for node in soup(html).select('a[href]'):
        try:
            url = safe_url(node['href'], COURSE_PATHS)
        except ValueError:
            continue
        match = re.fullmatch('/customer/' + re.escape(section) + r'/(\d+)', urlsplit(url).path)
        if match:
            result[match[1]] = {'id': match[1], 'title': text_of(node), 'url': url}
    return list(result.values())


def lesson_links(html):
    result = {}
    for node in soup(html).select('a[href]'):
        try:
            url = safe_url(node['href'], COURSE_PATHS)
        except ValueError:
            continue
        match = re.fullmatch(r'/customer/reading-courses/details/(\d+)', urlsplit(url).path)
        label = text_of(node)
        if not match or label.lower() in {'continue reading', 'next', 'prev', 'previous'}:
            continue
        ident = match[1]
        # Category headers and the first leaf share a route; prefer the leaf label
        # and its more specific query while preserving source order.
        previous = result.get(ident)
        if previous is None or len(urlsplit(url).query) >= len(urlsplit(previous['url']).query):
            result[ident] = {'id': ident, 'title': label, 'url': url}
    return list(result.values())


def audio_links(html):
    result = {}
    for node in soup(html).select('a[href]'):
        try:
            url = safe_url(node['href'], COURSE_PATHS)
        except ValueError:
            continue
        match = re.fullmatch(r'/customer/audio-courses/(\d+)/play', urlsplit(url).path)
        if match:
            result[match[1]] = {'id': match[1], 'title': text_of(node), 'url': url}
    return list(result.values())


def resource_links(html):
    result = {}
    for node in soup(html).select('a[href]'):
        url = urljoin(ORIGIN, node['href'])
        if not urlsplit(url).path.lower().endswith('.pdf'):
            continue
        url = asset_url(url)
        result[url] = {'id': hashlib.sha256(url.encode()).hexdigest()[:12], 'title': text_of(node), 'url': url}
    return list(result.values())


def reading_content(html):
    doc = soup(html)
    body = doc.select_one('.tabContent-area')
    if body is None:
        raise ValueError('Reading page lacks its lesson body')
    title = text_of(body.select_one('.reading-title'))
    for node in list(body.select('script,style,form,input,button,iframe,object,embed')):
        node.decompose()
    for node in list(body.select('.row')):
        if text_of(node) in {'Prev Next', 'Previous Next', 'Prev', 'Next'}:
            node.decompose()
    images = []
    for node in body.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith('on') or attr in {'srcset', 'data-src', 'data-srcset'}:
                del node[attr]
        if node.get('style') and re.search(r'url\s*\(', node['style'], re.I):
            del node['style']
        if node.name == 'a' and node.get('href', '').lower().startswith(('javascript:', 'data:')):
            del node['href']
        if node.name == 'img' and node.get('src'):
            node['src'] = asset_url(node['src'])
            images.append(node['src'])
    if len(text_of(body)) < 15:
        raise ValueError('Reading lesson has no usable text')
    text = '\n'.join(line.strip() for line in body.get_text('\n').splitlines() if line.strip())
    return {'title': title, 'html': str(body), 'text': text, 'images': list(dict.fromkeys(images))}


def audio_source(html):
    node = soup(html).select_one('audio source[src], audio[src]')
    if node is None:
        raise ValueError('Audio page has no downloadable source')
    url = asset_url(node['src'])
    if not urlsplit(url).path.lower().endswith('.mp3'):
        raise ValueError('Audio source is not an MP3')
    return url


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def valid_existing(root, entry):
    if entry.get('status') != 'complete' or not entry.get('sha256'):
        return False
    path = root / entry['path']
    return path.is_file() and path.stat().st_size == entry.get('bytes') and file_hash(path) == entry['sha256']


def validate_file(path, kind):
    if path.stat().st_size < 16:
        raise ValueError('Downloaded file is empty or too short')
    with path.open('rb') as stream:
        prefix = stream.read(16)
    if prefix.lstrip().lower().startswith((b'<html', b'<!doctype')):
        raise ValueError('Download returned an HTML page instead of media')
    extra = {}
    if kind == 'pdf':
        from pypdf import PdfReader
        if not prefix.startswith(b'%PDF-'):
            raise ValueError('Download is not a PDF')
        extra['pages'] = len(PdfReader(path).pages)
        if extra['pages'] < 1:
            raise ValueError('PDF contains no pages')
    elif kind == 'audio':
        from mutagen.mp3 import MP3
        info = MP3(path).info
        if not math.isfinite(info.length) or info.length <= 0:
            raise ValueError('Audio contains no playable duration')
        extra['duration_seconds'] = round(info.length, 3)
    elif kind == 'image':
        if path.read_bytes().lstrip().startswith(b'<svg') or path.suffix == '.svg':
            import xml.etree.ElementTree as ET
            if ET.parse(path).getroot().tag.rsplit('}', 1)[-1] != 'svg':
                raise ValueError('Invalid SVG image')
        else:
            from PIL import Image
            with Image.open(path) as picture:
                extra['width'], extra['height'] = picture.size
                picture.verify()
    return extra


class MaterialBrowser(BrowserTransport):
    def __init__(self, endpoint, delay):
        super().__init__(endpoint, delay, COURSE_PATHS)
        self.frame = self.call('Page.getFrameTree', {})['frameTree']['frame']['id']

    def request(self, method, url, data=None):
        for attempt in range(3):
            try:
                return super().request(method, url, data)
            except ValueError as error:
                if method != 'GET' or 'HTTP 403' not in str(error) or attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def download(self, url, path, max_bytes):
        url = asset_url(url)
        resource = self.call('Network.loadNetworkResource', {
            'frameId': self.frame, 'url': url,
            'options': {'disableCache': False, 'includeCredentials': urlsplit(url).hostname == 'www.contractorsischool.com'},
        })['resource']
        handle = resource.get('stream')
        try:
            if not resource.get('success') or resource.get('httpStatusCode') != 200 or not handle:
                raise ValueError('Browser download returned HTTP ' + str(resource.get('httpStatusCode')))
            headers = {k.lower(): str(v) for k, v in resource.get('headers', {}).items()}
            if int(headers.get('content-length', 0)) > max_bytes:
                raise ValueError('Download exceeds the file size limit')
            size = 0
            with path.open('wb') as output:
                while True:
                    part = self.call('IO.read', {'handle': handle, 'size': 256 * 1024})
                    block = base64.b64decode(part['data']) if part.get('base64Encoded') else part['data'].encode('utf-8')
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError('Download exceeds the file size limit')
                    output.write(block)
                    if part.get('eof'):
                        break
            return headers
        finally:
            if handle:
                self.call('IO.close', {'handle': handle})


def download_file(url, destination, kind, browser=None, max_bytes=512 * 1024 * 1024, browser_first=False):
    url = asset_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.part')
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        transport = 'direct'
        if browser_first and browser:
            headers = browser.download(url, temp, max_bytes)
            transport = 'browser'
        else:
            try:
                response = session.get(url, stream=True, timeout=(15, 90))
            except requests.RequestException:
                if browser is None:
                    raise
            if response is None or (response.status_code in {401, 403} and browser):
                if response is not None:
                    response.close()
                headers = browser.download(url, temp, max_bytes)
                transport = 'browser'
            elif response.status_code == 200:
                asset_url(response.url)
                headers = {k.lower(): str(v) for k, v in response.headers.items()}
                declared = int(headers.get('content-length', 0))
                if declared > max_bytes:
                    raise ValueError('Download exceeds the file size limit')
                if shutil.disk_usage(destination.parent).free < max(declared, 1024 * 1024) + 100 * 1024 * 1024:
                    raise ValueError('Insufficient free disk space for download')
                written = 0
                with temp.open('wb') as output:
                    for block in response.iter_content(256 * 1024):
                        written += len(block)
                        if written > max_bytes:
                            raise ValueError('Download exceeds the file size limit')
                        output.write(block)
            else:
                raise ValueError('Download returned HTTP ' + str(response.status_code))
        size = temp.stat().st_size
        declared = int(headers.get('content-length', 0))
        if declared and not headers.get('content-encoding') and size != declared:
            raise ValueError('Download byte count does not match Content-Length')
        extra = validate_file(temp, kind)
        result = {'bytes': size, 'sha256': file_hash(temp), 'transport': transport, **extra}
        temp.replace(destination)
        return result
    finally:
        if response is not None:
            response.close()
        session.close()
        if temp.exists():
            temp.unlink()


STYLE = 'body{max-width:980px;margin:32px auto;padding:0 24px;font:17px/1.65 Arial,sans-serif;color:#172b3a}a{color:#175e91}img{max-width:100%;height:auto}table{border-collapse:collapse;max-width:100%}td,th{border:1px solid #b8c5ce;padding:8px}section{margin:48px 0;border-top:1px solid #ccd6dd;padding-top:20px}nav li{margin:5px 0}h1,h2,h3{line-height:1.25}.reading-title h5{font-size:24px;margin:8px 0 20px}'


def document(title, body):
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + escape(title) + '</title><style>' + STYLE + '</style></head><body>' + body + '</body></html>'


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        filename = self.root / 'manifest.json'
        self.data = json.loads(filename.read_text(encoding='utf-8')) if filename.exists() else {'schema_version': 1, 'items': [], 'courses': []}
        self.by_key = {item['key']: item for item in self.data['items']}

    def item(self, key, **fields):
        item = self.by_key.get(key)
        if item is None:
            item = {'key': key, 'status': 'pending'}
            self.by_key[key] = item
            self.data['items'].append(item)
        item.update(fields)
        return item

    def save(self):
        self.data['updated_at'] = datetime.now(timezone.utc).isoformat()
        self.data['summary'] = {kind: dict(total=sum(i['kind'] == kind for i in self.data['items']), complete=sum(i['kind'] == kind and i['status'] == 'complete' for i in self.data['items'])) for kind in ['reading', 'audio', 'pdf', 'image']}
        temp = self.root / 'manifest.json.tmp'
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(self.root / 'manifest.json')

    def asset(self, url):
        url = asset_url(url)
        ident = hashlib.sha256(url.encode()).hexdigest()[:20]
        ext = Path(urlsplit(url).path).suffix.lower() or '.png'
        return self.item('image:' + ident, kind='image', title=ident, source_url=url, path='assets/' + ident + ext)

    def reading(self, browser, course, lesson, order):
        base = 'reading/' + slug(course['title'])
        relative = base + '/chapters/' + f"{order:03d}-{lesson['id']}-{slug(lesson['title'])}.html"
        item = self.item('reading:' + course['id'] + ':' + lesson['id'], kind='reading', course=course['title'], course_id=course['id'], order=order, id=lesson['id'], title=lesson['title'], source_url=lesson['url'], path=relative)
        if valid_existing(self.root, item):
            return
        content = reading_content(browser.request('GET', lesson['url']))
        item.update(title=content['title'] or lesson['title'], text=content['text'], content_html=content['html'], image_urls=content['images'])
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document(item['title'], content['html']), encoding='utf-8')
        text_path = path.with_suffix('.txt')
        text_path.write_text(content['text'] + '\n', encoding='utf-8')
        item.update(status='complete', bytes=path.stat().st_size, sha256=file_hash(path), text_path=text_path.relative_to(self.root).as_posix())
        for url in content['images']:
            self.asset(url)
        self.save()

    def render(self):
        assets = {i['source_url']: i for i in self.data['items'] if i['kind'] == 'image' and i['status'] == 'complete'}
        index = ['<h1>Contractors iSchool study materials</h1><p>Downloaded reading, audio and resources.</p>']
        if (self.root.parent / 'questions.csv').exists():
            index.append('<p>Practice questions: <a href="../questions.csv">CSV</a> · <a href="../questions.json">JSON</a></p>')
        for course in self.data['courses']:
            title = course['title']
            index.append('<h2>' + escape(title) + '</h2><ul>')
            readings = sorted((i for i in self.data['items'] if i['kind'] == 'reading' and i['course_id'] == course['id']), key=lambda i: i['order'])
            if readings:
                combined, toc, texts = [], [], []
                book = self.root / 'reading' / slug(title) / 'index.html'
                for item in readings:
                    for target, prefix in [(self.root / item['path'], '../../../'), (book, '../../')]:
                        body = soup(item['content_html'])
                        for node in body.select('img[src]'):
                            asset = assets.get(node['src'])
                            if asset:
                                node['src'] = prefix + asset['path']
                        if target == book:
                            combined.append('<section id="lesson-' + item['id'] + '">' + str(body) + '</section>')
                        else:
                            target.write_text(document(item['title'], str(body)), encoding='utf-8')
                            item.update(bytes=target.stat().st_size, sha256=file_hash(target))
                    toc.append('<li><a href="#lesson-' + item['id'] + '">' + escape(item['title']) + '</a></li>')
                    texts.append(item['title'] + '\n' + item['text'])
                book.write_text(document(title, '<h1>' + escape(title) + '</h1><nav><ol>' + ''.join(toc) + '</ol></nav>' + ''.join(combined)), encoding='utf-8')
                book.with_suffix('.txt').write_text('\n\n'.join(texts), encoding='utf-8')
                index.append('<li><a href="' + book.relative_to(self.root).as_posix() + '">Reading course — ' + str(len(readings)) + ' lessons</a></li>')
            audio = sorted((i for i in self.data['items'] if i['kind'] == 'audio' and i['course_id'] == course['id']), key=lambda i: i['order'])
            if audio:
                playlist = self.root / 'audio' / slug(title) / 'playlist.m3u'
                playlist.parent.mkdir(parents=True, exist_ok=True)
                lines = ['#EXTM3U']
                players = ['<h1>' + escape(title) + ' — Audio Course</h1><p><a href="../../index.html">Study library</a> · <a href="playlist.m3u">Playlist</a></p><p>MP3 files for your audio player. Keep the playlist in this folder with its tracks.</p><ol>']
                for item in audio:
                    if item['status'] == 'complete':
                        lines += ['#EXTINF:' + str(round(item.get('duration_seconds', 0))) + ',' + item['title'], Path(item['path']).name]
                        filename = Path(item['path']).name
                        players.append('<li><a href="' + filename + '" download>' + escape(item['title']) + ' — MP3 file</a></li>')
                playlist.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                audio_page = playlist.with_name('index.html')
                audio_page.write_text(document(title + ' audio', ''.join(players) + '</ol>'), encoding='utf-8')
                index.append('<li><a href="' + audio_page.relative_to(self.root).as_posix() + '">Audio course — ' + str(len(audio)) + ' tracks</a></li>')
            for item in self.data['items']:
                if item['kind'] == 'pdf' and item['course_id'] == course['id'] and item['status'] == 'complete':
                    index.append('<li><a href="' + item['path'] + '">' + escape(item['title']) + '</a> (' + str(item['pages']) + ' pages)</li>')
            index.append('</ul>')
        (self.root / 'index.html').write_text(document('Study materials', ''.join(index)), encoding='utf-8')
        self.save()


def run(browser, library, workers=4, max_bytes=512 * 1024 * 1024):
    courses = {}
    for section, kind in [('reading-courses', 'reading'), ('audio-courses', 'audio'), ('reading-resources', 'pdf')]:
        index = browser.request('GET', ORIGIN + '/customer/' + section)
        entries = course_links(index, section)
        if not entries:
            raise ValueError('No courses found in ' + section)
        for course in entries:
            courses[course['id']] = {'id': course['id'], 'title': course['title']}
            page = browser.request('GET', course['url'])
            items = lesson_links(page) if kind == 'reading' else audio_links(page) if kind == 'audio' else resource_links(page)
            if not items:
                raise ValueError('Course contains no ' + kind + ' entries: ' + course['title'])
            print('Discover: ' + kind + ' / ' + course['title'] + ' / ' + str(len(items)), flush=True)
            for order, item in enumerate(items, 1):
                if kind == 'reading':
                    library.reading(browser, course, item, order)
                else:
                    ext = '.mp3' if kind == 'audio' else '.pdf'
                    relative = ('audio/' if kind == 'audio' else 'resources/') + slug(course['title']) + '/' + f"{order:03d}-{item['id']}-{slug(item['title'])}" + ext
                    entry = library.item(kind + ':' + course['id'] + ':' + item['id'], kind=kind, course=course['title'], course_id=course['id'], order=order, title=item['title'], path=relative, page_url=item['url'])
                    if kind == 'pdf':
                        entry['source_url'] = item['url']
                    elif not entry.get('source_url'):
                        entry['source_url'] = audio_source(browser.request('GET', item['url']))
                    library.save()
    # Reading/audio section titles have friendlier labels than Resources' account labels.
    for ident, title in [('10', 'Law & Business'), ('13', 'C10 Electrical Contractor'), ('21', 'Math & Safety Review')]:
        if ident in courses:
            courses[ident]['title'] = title
    library.data['courses'] = list(courses.values())
    library.save()
    downloads = [i for i in library.data['items'] if i['kind'] in {'audio', 'image'} and not valid_existing(library.root, i)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_file, i['source_url'], library.root / i['path'], i['kind'], None, max_bytes): i for i in downloads}
        for future in as_completed(futures):
            item = futures[future]
            try:
                item.update(future.result(), status='complete')
                item.pop('error', None)
            except Exception as error:
                item.update(status='failed', error=type(error).__name__ + ': ' + str(error))
            library.save()
            print('Download: ' + item['kind'] + ' / ' + item['title'] + ' / ' + item['status'], flush=True)
    for item in library.data['items']:
        if item['kind'] != 'pdf' or valid_existing(library.root, item):
            continue
        try:
            item.update(download_file(item['source_url'], library.root / item['path'], 'pdf', browser, max_bytes, browser_first=urlsplit(item['source_url']).hostname.removeprefix('www.') == 'cslb.ca.gov'), status='complete')
            item.pop('error', None)
        except Exception as error:
            item.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        library.save()
        print('Resource: ' + item['title'] + ' / ' + item['status'], flush=True)
    library.render()
    return sum(item['status'] != 'complete' for item in library.data['items'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cdp', default='http://127.0.0.1:9333')
    parser.add_argument('--output', type=Path, default=Path('exports/contractors-school/materials'))
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--delay', type=float, default=1.0)
    parser.add_argument('--max-file-mb', type=int, default=512)
    args = parser.parse_args()
    if args.workers not in range(1, 9) or args.delay < 0 or args.max_file_mb < 1:
        parser.error('Use one to eight workers, a nonnegative delay and a positive file size limit')
    library = Library(args.output)
    try:
        failures = run(MaterialBrowser(args.cdp, args.delay), library, args.workers, args.max_file_mb * 1024 * 1024)
    except Exception as error:
        library.save()
        print('Collection incomplete: ' + type(error).__name__ + ': ' + str(error), flush=True)
        return 2
    print('SUMMARY ' + json.dumps(library.data['summary']), flush=True)
    print('INDEX ' + str((args.output / 'index.html').resolve()), flush=True)
    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
