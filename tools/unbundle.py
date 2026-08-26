"""Turn an exported single-file bundle into a fast multi-file site.

The authoring tool exports one self-contained HTML with every font, image and
video inlined as base64. The browser has to download and decode the whole thing
before it can paint anything, so a 25MB export means a blank screen until 25MB
has arrived. This unpacks the assets into real files the browser can fetch in
parallel, stream and cache, and shrinks them on the way out:

  fonts   subset to the characters the page actually uses, ttf -> woff2
  images  png -> webp
  video   written out as-is (streams instead of blocking the HTML)

Usage:  python unbundle.py <exported-bundle.html> <output-dir> [--cta URL]

Requires: fonttools[woff] (brotli), pillow.
"""
import argparse
import base64
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys

from fontTools import subset
from PIL import Image

UUID_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# Resolved through window.__resources by the page runtime; without the map it
# falls back to the CDN and additionally re-fetches the page looking for bundle
# data. Only present in exports that ship the runtime.
CDN_LOCAL = {
    'https://unpkg.com/react@18.3.1/umd/react.production.min.js': 'js/react.js',
    'https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js': 'js/react-dom.js',
}

FONT_MIME = {'font/woff2': 'woff2', 'font/woff': 'woff', 'font/ttf': 'ttf',
             'font/otf': 'otf', 'application/font-woff': 'woff'}


def read_block(doc, name, default=None):
    tag = '<script type="__bundler/%s">' % name
    if tag not in doc:
        return default
    s = doc.index(tag) + len(tag)
    return json.loads(doc[s:doc.index('</script>', s)].strip())


def load_assets(doc):
    """uuid -> (mime, raw bytes), gunzipped where the bundle compressed them."""
    out = {}
    for uuid, entry in read_block(doc, 'manifest').items():
        raw = base64.b64decode(entry['data'])
        if entry.get('compressed'):
            raw = gzip.decompress(raw)
        out[uuid] = (entry['mime'], raw)
    return out


def page_charset(tpl):
    """Every character the markup can render, plus ASCII and common punctuation.

    Style blocks are stripped first so uuid soup and CSS keywords don't drag
    unused glyphs into the subset.
    """
    text = re.sub(r'<style>.*?</style>', ' ', tpl, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    chars = set(text) | set(chr(c) for c in range(0x20, 0x7F))
    chars |= set('—–…·“”‘’「」『』€₩©®™✓×°※')
    return sorted(ord(c) for c in chars if ord(c) > 0x1F)


def do_fonts(tpl, assets, out, unicodes, log):
    """Subset every font to the page charset and normalise it to woff2.

    A face can be split across unicode-ranges and several faces can share one
    file, so this works per source file (uuid), not per @font-face block.
    """
    font_uuids = [u for u, (mime, _) in assets.items() if mime in FONT_MIME]
    if not font_uuids:
        return tpl
    os.makedirs(os.path.join(out, 'fonts'), exist_ok=True)

    before = after = 0
    for uuid in font_uuids:
        mime, raw = assets[uuid]
        tmp = os.path.join(out, 'fonts', '_src-%s.%s' % (uuid[:8], FONT_MIME[mime]))
        with open(tmp, 'wb') as f:
            f.write(raw)

        # Name it after the family that references it, so the folder is legible.
        m = re.search(r'font-family:\s*[\'"]?([^;\'"]+)[\'"]?;[^}]*%s' % uuid, tpl, re.S)
        if not m:
            m = re.search(r'%s[^}]*?font-family:\s*[\'"]?([^;\'"]+)' % uuid, tpl, re.S)
        family = re.sub(r'[^A-Za-z0-9]+', '-', m.group(1).strip()).strip('-').lower() if m else 'font'
        name = '%s-%s.woff2' % (family, uuid[:8])
        dst = os.path.join(out, 'fonts', name)

        try:
            subset.main([
                tmp, '--flavor=woff2',
                '--unicodes=%s' % ','.join('U+%04X' % u for u in unicodes),
                '--layout-features=*',
                '--output-file=%s' % dst,
            ])
        except Exception as err:                      # keep the original rather than lose glyphs
            log('  ! subset failed for %s (%s) — copying as-is' % (name, err))
            dst = os.path.join(out, 'fonts', '%s-%s.%s' % (family, uuid[:8], FONT_MIME[mime]))
            with open(dst, 'wb') as f:
                f.write(raw)
        os.remove(tmp)

        before += len(raw)
        after += os.path.getsize(dst)
        tpl = tpl.replace(uuid, 'fonts/' + os.path.basename(dst))

    # The declared format has to follow the conversion or the browser skips the file.
    tpl = re.sub(r'(url\("fonts/[^"]+\.woff2"\)\s*format\()([\'"])truetype\2',
                 r'\1\2woff2\2', tpl)
    log('fonts: %s -> %s' % (fmt(before), fmt(after)))
    return tpl


def do_images(tpl, assets, out, log):
    uuids = [u for u, (mime, _) in assets.items() if mime.startswith('image/')]
    if not uuids:
        return tpl
    os.makedirs(os.path.join(out, 'img'), exist_ok=True)

    before = after = 0
    for uuid in uuids:
        mime, raw = assets[uuid]
        src = os.path.join(out, 'img', '_src-%s' % uuid[:8])
        with open(src, 'wb') as f:
            f.write(raw)
        im = Image.open(src)
        name = 'img/%s.webp' % uuid[:8]
        im.save(os.path.join(out, name), 'WEBP', quality=88, method=6)
        os.remove(src)

        before += len(raw)
        after += os.path.getsize(os.path.join(out, name))

        # width/height let the browser reserve the right space before the file
        # lands. These pages scale images with `width:100%`, so height must be
        # released to auto as well — otherwise the height attribute becomes the
        # used height and stretches the image to its full natural pixel height.
        def fix_img(m, name=name, im=im):
            tag = m.group(0)
            style = re.search(r'style="([^"]*)"', tag)
            if style and 'width' in style.group(1) and not re.search(r'[^-]height\s*:', ';' + style.group(1)):
                tag = tag.replace(style.group(0),
                                  'style="%s;height:auto"' % style.group(1).rstrip('; '))
            return tag.replace('src="%s"' % uuid,
                               'src="%s" width="%d" height="%d" decoding="async"'
                               % (name, im.width, im.height))

        tpl = re.sub(r'<img[^>]*src="%s"[^>]*>' % uuid, fix_img, tpl)
        tpl = tpl.replace(uuid, name)
    log('images: %s -> %s' % (fmt(before), fmt(after)))
    return tpl


def do_video(tpl, assets, out, ffmpeg, log):
    """Write videos out as files and, if ffmpeg is around, re-encode them.

    The exports carry background clips at ~4 Mbit/s with an audio track the
    markup mutes anyway, so dropping audio and re-encoding at CRF 24 cuts them
    several-fold with no visible difference.
    """
    uuids = [u for u, (mime, _) in assets.items() if mime.startswith('video/')]
    if not uuids:
        return tpl
    os.makedirs(os.path.join(out, 'video'), exist_ok=True)

    before = after = 0
    for uuid in uuids:
        mime, raw = assets[uuid]
        ext = mime.split('/')[-1]
        name = 'video/%s.%s' % (uuid[:8], ext)
        dst = os.path.join(out, name)
        with open(dst, 'wb') as f:
            f.write(raw)
        before += len(raw)

        muted = re.search(r'<video[^>]*%s[^>]*>' % uuid, tpl)
        muted = bool(muted and 'muted' in muted.group(0))
        if ffmpeg:
            tmp = dst + '.tmp.mp4'
            cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', dst,
                   '-map', '0:v:0', '-c:v', 'libx264', '-crf', '24', '-preset', 'slow',
                   '-profile:v', 'main', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
            cmd += ['-an'] if muted else ['-map', '0:a?', '-c:a', 'aac', '-b:a', '96k']
            cmd += [tmp]
            if subprocess.call(cmd) == 0 and os.path.getsize(tmp) < os.path.getsize(dst):
                os.replace(tmp, dst)
            elif os.path.exists(tmp):
                os.remove(tmp)
        after += os.path.getsize(dst)
        tpl = tpl.replace(uuid, name)

    log('video: %s -> %s%s' % (fmt(before), fmt(after),
                               '' if ffmpeg else '  (ffmpeg not found — copied as-is)'))
    return tpl


def do_scripts(tpl, assets, out, log):
    uuids = [u for u, (mime, _) in assets.items() if mime == 'text/javascript']
    if not uuids:
        return tpl
    os.makedirs(os.path.join(out, 'js'), exist_ok=True)

    runtime = next((u for u in uuids if u in tpl), None)
    others = sorted((u for u in uuids if u != runtime), key=lambda u: len(assets[u][1]))
    names = {runtime: 'dc-runtime.js'}
    for u, n in zip(others, ('react.js', 'react-dom.js')):
        names[u] = n
    for u in uuids:
        names.setdefault(u, '%s.js' % u[:8])
        with open(os.path.join(out, 'js', names[u]), 'wb') as f:
            f.write(assets[u][1])

    if runtime:
        tpl = tpl.replace(
            '<script src="%s"></script>' % runtime,
            '<script>window.__resources = %s;</script>\n<script src="js/dc-runtime.js"></script>'
            % json.dumps(CDN_LOCAL))
    log('scripts: %d file(s)' % len(uuids))
    return tpl


def do_viewport(tpl, log):
    """Fixed-width artboards need their width declared or phones don't scale them."""
    m = re.search(r'<meta name="viewport" content="([^"]*)">', tpl)
    if not m or 'device-width' not in m.group(1):
        log('viewport: left as-is (%s)' % (m.group(1) if m else 'none'))
        return tpl
    widths = re.findall(r'width:\s*(\d{3,4})px;margin:0 auto', tpl)
    if not widths:
        log('viewport: device-width kept — no fixed-width artboard found')
        return tpl
    board = max(int(w) for w in widths)
    tpl = tpl.replace(m.group(0), '<meta name="viewport" content="width=%d">' % board)
    log('viewport: device-width -> width=%d (artboard)' % board)
    return tpl


def do_cta(tpl, url, log):
    """The export renders the bottom button as a bare div; make it link out."""
    if not url:
        return tpl
    if 'href="%s"' % url in tpl:
        log('cta: already linked')
        return tpl
    m = re.search(r'<div style="background:#6e1414[^"]*">\s*([^<]*?)\s*</div>', tpl, re.S)
    if not m:
        log('cta: button not found — skipped')
        return tpl
    tpl = tpl.replace(m.group(0),
                      '<a href="%s" target="_blank" rel="noopener" '
                      'style="display:block;text-decoration:none;color:inherit;cursor:pointer;">'
                      '%s</a>' % (url, m.group(0)))
    log('cta: linked to %s' % url)
    return tpl


def fmt(n):
    return '%s bytes' % format(n, ',')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('bundle')
    ap.add_argument('out')
    ap.add_argument('--cta', default=None, help='URL for the bottom CTA button')
    ap.add_argument('--ffmpeg', default=None, help='path to ffmpeg (default: PATH)')
    a = ap.parse_args(argv)

    with io.open(a.bundle, encoding='utf-8') as f:
        doc = f.read()

    assets = load_assets(doc)
    tpl = read_block(doc, 'template')
    os.makedirs(a.out, exist_ok=True)

    def log(msg):
        print(msg)

    log('%s  (%s)' % (os.path.basename(a.bundle), fmt(os.path.getsize(a.bundle))))
    tpl = do_fonts(tpl, assets, a.out, page_charset(tpl), log)
    tpl = do_images(tpl, assets, a.out, log)
    tpl = do_video(tpl, assets, a.out, a.ffmpeg or shutil.which('ffmpeg'), log)
    tpl = do_scripts(tpl, assets, a.out, log)
    tpl = do_viewport(tpl, log)
    tpl = do_cta(tpl, a.cta, log)

    leftover = set(re.findall(UUID_RE, tpl))
    assert not leftover, 'unresolved resource ids: %s' % leftover

    with io.open(os.path.join(a.out, 'index.html'), 'w', encoding='utf-8', newline='') as f:
        f.write(tpl)
    open(os.path.join(a.out, '.nojekyll'), 'w').close()

    total = sum(os.path.getsize(os.path.join(r, n))
                for r, _, ns in os.walk(a.out) for n in ns)
    log('index.html: %s' % fmt(len(tpl.encode('utf-8'))))
    log('site total: %s' % fmt(total))


if __name__ == '__main__':
    main()
