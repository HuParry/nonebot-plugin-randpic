"""静态网页发布：原子替换文件、分页与图片懒加载。"""

import hashlib
import math
from html import escape
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Set
from urllib.parse import quote

from .models import ImageRecord, PublishedFile, RandpicError
from .utils import atomic_write, digest_file, parse_index, safe_path

GALLERY_STYLE = '''
    :root { color-scheme: light; --ink: #18201d; --muted: #758079; --paper: #f5f6f2; --card: #fff; --line: #e7eae4; --accent: #286b55; --accent-soft: #e7f1eb; }
    * { box-sizing: border-box; }
    body { margin: 0; color: var(--ink); background: var(--paper); font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
    a { color: inherit; }
    .shell { width: min(1160px, calc(100% - 48px)); margin: 0 auto; }
    .topbar { height: 72px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--line); }
    .brand { display: inline-flex; align-items: center; gap: 11px; text-decoration: none; font-weight: 750; letter-spacing: -.03em; }
    .brand-mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 11px; color: white; background: var(--accent); font-size: 17px; }
    .top-note { color: var(--muted); font-size: 13px; }
    .hero { padding: 58px 0 34px; }
    .eyebrow { margin: 0 0 12px; color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 0; font-size: clamp(32px, 5vw, 52px); line-height: 1.08; letter-spacing: -.055em; }
    .hero-copy { max-width: 620px; margin: 15px 0 0; color: var(--muted); font-size: 16px; line-height: 1.75; }
    .section-head { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 26px 0 17px; }
    .section-head h2 { margin: 0; font-size: 19px; letter-spacing: -.025em; }
    .count { color: var(--muted); font-size: 13px; }
    .folder-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(245px, 1fr)); gap: 17px; padding-bottom: 60px; }
    .folder-card { overflow: hidden; border: 1px solid var(--line); border-radius: 17px; background: var(--card); text-decoration: none; transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease; }
    .folder-card:hover { transform: translateY(-4px); border-color: #c9d9ce; box-shadow: 0 14px 34px #24392b12; }
    .folder-cover { position: relative; height: 172px; overflow: hidden; background: linear-gradient(135deg, #dce9df, #f0eadd); }
    .folder-cover img { width: 100%; height: 100%; object-fit: cover; transition: transform .35s ease; }
    .folder-card:hover .folder-cover img { transform: scale(1.045); }
    .cover-placeholder { height: 100%; display: grid; place-items: center; color: #668474; font-size: 46px; }
    .folder-body { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 17px 18px; }
    .folder-name { overflow: hidden; font-size: 15px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
    .folder-meta { margin-top: 5px; color: var(--muted); font-size: 12px; }
    .arrow { flex: none; width: 31px; height: 31px; display: grid; place-items: center; border-radius: 50%; color: var(--accent); background: var(--accent-soft); }
    .crumbs { display: flex; align-items: center; gap: 9px; margin-top: 27px; color: var(--muted); font-size: 13px; }
    .crumbs a { color: var(--accent); text-decoration: none; }
    .crumbs a:hover { text-decoration: underline; }
    .gallery-title { padding: 24px 0 22px; }
    .gallery-title h1 { font-size: clamp(30px, 4vw, 42px); }
    .subfolders { display: flex; flex-wrap: wrap; gap: 9px; margin: 0 0 22px; }
    .subfolder { padding: 9px 13px; border: 1px solid #dce5dc; border-radius: 999px; color: var(--accent); background: #fff; font-size: 13px; text-decoration: none; transition: background .2s ease; }
    .subfolder:hover { background: var(--accent-soft); }
    .image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 17px; padding-bottom: 60px; }
    .image-card { overflow: hidden; border: 1px solid var(--line); border-radius: 15px; background: var(--card); transition: transform .2s ease, box-shadow .2s ease; }
    .image-card:hover { transform: translateY(-3px); box-shadow: 0 12px 28px #24392b12; }
    .image-link { position: relative; display: block; height: 205px; overflow: hidden; background: #e9ece7; }
    .image-preview { width: 100%; height: 100%; display: block; object-fit: cover; transition: transform .35s ease; }
    .image-card:hover .image-preview { transform: scale(1.04); }
    .image-info { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 14px; }
    .image-name { min-width: 0; overflow: hidden; color: #48534c; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
    .open-link { flex: none; color: var(--accent); font-size: 12px; font-weight: 700; text-decoration: none; }
    .empty { grid-column: 1 / -1; padding: 50px 20px; border: 1px dashed #cbd4ca; border-radius: 16px; color: var(--muted); text-align: center; }
    footer { padding: 22px 0 30px; border-top: 1px solid var(--line); color: #929b94; font-size: 12px; text-align: center; }
    @media (max-width: 600px) { .shell { width: min(100% - 30px, 1160px); } .topbar { height: 62px; } .top-note { font-size: 11px; } .hero { padding: 42px 0 24px; } .folder-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; } .folder-cover { height: 125px; } .folder-body { padding: 12px; } .folder-name { font-size: 13px; } .arrow { width: 27px; height: 27px; } .image-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; } .image-link { height: 145px; } .image-info { padding: 10px; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; } }
'''


def _url(image: ImageRecord) -> str:
    return quote(image.filename, safe='') + '?v=' + image.content_hash[:16]


def _shell(title: str, home: str, content: str) -> str:
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f5f6f2">
<title>{escape(title)} · Randpic</title><style>{GALLERY_STYLE}
.pagination {{display:flex;gap:14px;justify-content:center;align-items:center;margin:0 0 35px}}
.pagination a {{color:var(--accent);text-decoration:none}} .pagination span {{color:var(--muted)}}
</style></head><body><div class="shell">
<nav class="topbar"><a class="brand" href="{home}"><span class="brand-mark">✳</span><span>RANDPIC / 图片收藏馆</span></a><span class="top-note">捕捉喜欢的瞬间</span></nav>
{content}<footer>Randpic · 收藏每一个喜欢的瞬间</footer></div>
</body></html>'''


def render_catalog(groups: Mapping[str, Sequence[ImageRecord]], page_size: int) -> Dict[str, bytes]:
    """生成纯静态 HTML，只让内容实际变化的页面产生同步任务。"""
    pages = {command: max(1, math.ceil(len(images) / page_size)) for command, images in groups.items()}
    files: Dict[str, bytes] = {}
    total = sum(len(images) for images in groups.values())
    cards = []
    for command, images in sorted(groups.items()):
        folder_url = './' + quote(command, safe='') + '/'
        cover = (f'<img src="{escape(folder_url + _url(images[0]), quote=True)}" alt="" loading="lazy">'
                 if images else '<div class="cover-placeholder">✳</div>')
        cards.append(f'''<a class="folder-card" href="{escape(folder_url, quote=True)}"><div class="folder-cover">{cover}</div>
<div class="folder-body"><div><div class="folder-name">{escape(command)}</div><div class="folder-meta">{len(images)} 张图片</div></div><span class="arrow">↗</span></div></a>''')
    if not cards:
        cards.append('<div class="empty">图片库还是空的，添加图片后就会显示在这里。</div>')
    home = f'''<header class="hero"><p class="eyebrow">YOUR IMAGE LIBRARY</p><h1>每一张，都值得<br>被好好收藏。</h1>
<p class="hero-copy">在这里慢慢翻看收藏的每一份灵感。</p></header><div class="section-head"><h2>全部分类</h2>
<span class="count">{len(groups)} 个分类　·　{total} 张图片</span></div><main class="folder-grid">{''.join(cards)}</main>'''
    files['index.html'] = _shell('图片收藏馆', './', home).encode()
    for command, images in groups.items():
        for page in range(1, pages[command] + 1):
            cards = []
            for image in images[(page - 1) * page_size:page * page_size]:
                url, name = escape(_url(image), quote=True), escape(image.filename)
                cards.append(f'''<article class="image-card"><a class="image-link" href="{url}" target="_blank" rel="noopener">
<img class="image-preview" src="{url}" alt="{name}" loading="lazy"></a><div class="image-info"><span class="image-name" title="{name}">{name}</span>
<a class="open-link" href="{url}" target="_blank" rel="noopener">查看 ↗</a></div></article>''')
            if not cards:
                cards.append('<div class="empty">这个分类暂时还没有图片。</div>')
            navigation = []
            if page > 1:
                previous = './' if page == 2 else f'./page-{page - 1}.html'
                navigation.append(f'<a href="{previous}">← 上一页</a>')
            navigation.append(f'<span>第 {page} / {pages[command]} 页</span>')
            if page < pages[command]:
                navigation.append(f'<a href="./page-{page + 1}.html">下一页 →</a>')
            content = f'''<div class="crumbs"><a href="../">全部分类</a><span>／</span><span>{escape(command)}</span></div>
<header class="gallery-title"><p class="eyebrow">COLLECTION</p><h1>{escape(command)}</h1><p class="hero-copy">本分类共收藏 {len(images)} 张图片。</p></header>
<main class="image-grid">{''.join(cards)}</main><nav class="pagination">{''.join(navigation)}</nav>'''
            filename = 'index.html' if page == 1 else f'page-{page}.html'
            files[f'{command}/{filename}'] = _shell(command, '../', content).encode()
    return files


class GalleryPublisher:
    def __init__(self, root: Path, page_size: int = 100):
        self.root = root
        self.image_root = root / 'img'
        self.local_directory = root / 'public'
        self.remote_directory = root / 'database' / 'oss_pages'
        self.page_size = page_size
        self._asset_hashes: Dict[str, str] = {}
        self._asset_md5: Dict[str, str] = {}

    @staticmethod
    def _write_pages(directory: Path, pages: Mapping[str, bytes]) -> None:
        for key, data in pages.items():
            path = directory / key
            if path.is_symlink() or path.parent.is_symlink():
                raise RandpicError('网页输出目录不能包含符号链接。')
            if not path.exists() or path.read_bytes() != data:
                atomic_write(path, data)

    @staticmethod
    def _prune(directory: Path, wanted: Set[str]) -> None:
        for path in sorted(directory.rglob('*'), reverse=True):
            if path.is_symlink():
                path.unlink()
            elif path.is_file() and path.relative_to(directory).as_posix() not in wanted:
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def publish(self, images: Sequence[ImageRecord], commands: Iterable[str], excluded: Set[str], *, remote_enabled: bool = False) -> Dict[str, PublishedFile]:
        for directory in ((self.local_directory, self.remote_directory) if remote_enabled else (self.local_directory,)):
            if directory.is_symlink():
                raise RandpicError('网页输出目录不能是符号链接。')
            directory.mkdir(parents=True, exist_ok=True)
        groups: Dict[str, List[ImageRecord]] = {command: [] for command in sorted(commands)}
        for image in images:
            if image.command in groups:
                groups[image.command].append(image)
        for command in groups:
            groups[command].sort(key=lambda image: (parse_index(Path(image.filename).stem, command) or 0, image.filename))
        assets: Dict[str, str] = {}
        checksums: Dict[str, str] = {}
        files: Dict[str, PublishedFile] = {}
        for group in groups.values():
            for image in group:
                path = safe_path(self.local_directory, image.key)
                known = self._asset_hashes.get(image.key) == image.content_hash
                if not known or not path.exists() or path.stat().st_size != image.byte_size:
                    if not path.exists() or digest_file(path) != image.content_hash:
                        data = safe_path(self.image_root, image.key).read_bytes()
                        if hashlib.sha256(data).hexdigest() != image.content_hash:
                            raise RandpicError(f'图片文件在运行时被修改：{image.key}')
                        atomic_write(path, data)
                assets[image.key] = image.content_hash
                if remote_enabled and image.command not in excluded:
                    checksum = self._asset_md5.get(image.key) if known else None
                    if checksum is None:
                        checksum = digest_file(path, 'md5')
                    checksums[image.key] = checksum
                    files[image.key] = PublishedFile(str(path), image.content_hash, checksum, image.byte_size)
        local_pages = render_catalog(groups, self.page_size)
        self._write_pages(self.local_directory, local_pages)
        self._prune(self.local_directory, set(local_pages) | set(assets))
        self._asset_hashes, self._asset_md5 = assets, checksums
        if not remote_enabled:
            return {}
        remote_groups = {command: group for command, group in groups.items() if command not in excluded}
        remote_pages = render_catalog(remote_groups, self.page_size)
        self._write_pages(self.remote_directory, remote_pages)
        self._prune(self.remote_directory, set(remote_pages))
        manifest = {key: PublishedFile(
            str(self.remote_directory / key), hashlib.sha256(data).hexdigest(),
            hashlib.md5(data, usedforsecurity=False).hexdigest(), len(data),
        ) for key, data in remote_pages.items()}
        manifest.update({image.key: files[image.key]
                         for group in remote_groups.values() for image in group})
        return manifest
