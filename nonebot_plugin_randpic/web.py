import os
from html import escape
from pathlib import Path
from urllib.parse import quote

footer_content = '<a href="http://beian.miit.gov.cn/" target="_blank" rel="nofollow noopener">湘ICP备2025127872号-1</a>'
FOOTER = '</main><footer>{footer}</footer></div></body></html>'.format(footer=footer_content)

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


class StaticImageGalleryGenerator:
    def __init__(self, source_folder, output_folder):
        self.source_folder = Path(source_folder)
        self.output_folder = Path(output_folder)
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg'}

    def get_all_images_with_folders(self, no_upload_oss_list):
        """获取所有文件夹及其包含的图片文件"""
        result = {}

        if self.source_folder.exists():
            for root, dirs, files in os.walk(self.source_folder):
                # 计算相对路径
                rel_path = os.path.relpath(root, self.source_folder)
                if rel_path == '.':
                    folder_key = ''
                else:
                    folder_key = rel_path

                # 不上传oss的文件夹跳过
                if folder_key in no_upload_oss_list:
                    continue

                # 过滤图片文件
                image_files = []
                for filename in files:
                    if Path(filename).suffix.lower() in self.supported_formats:
                        image_files.append(filename)

                if image_files:  # 只有包含图片的文件夹才添加
                    result[folder_key] = {
                        'path': rel_path,
                        'images': sorted(image_files),
                        'image_count': len(image_files),
                        'subfolders': dirs
                    }

        return result

    def generate_index_html(self, all_folders):
        """生成主页HTML"""
        total_images = sum(folder['image_count'] for folder in all_folders.values())
        parts = [f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f5f6f2"><title>图片收藏馆 · Randpic</title>
<style>{GALLERY_STYLE}</style></head><body><div class="shell">
<nav class="topbar"><a class="brand" href="./"><span class="brand-mark">✳</span><span>RANDPIC <span style="color:#8b958e;font-weight:500">/ 图片收藏馆</span></span></a><span class="top-note">捕捉喜欢的瞬间</span></nav>
<header class="hero"><p class="eyebrow">YOUR IMAGE LIBRARY</p><h1>每一张，都值得<br>被好好收藏。</h1><p class="hero-copy">在这里慢慢翻看收藏的每一份灵感。</p></header>
<div class="section-head"><h2>全部分类</h2><span class="count">{len(all_folders)} 个分类　·　{total_images} 张图片</span></div>
<main class="folder-grid">''']

        if not all_folders:
            parts.append('<div class="empty">图片库还是空的，添加图片后就会显示在这里。</div>')
        for folder_key, folder_info in all_folders.items():
            display_name = folder_key or '根目录'
            folder_url = './' if not folder_key else f'./{quote(folder_key, safe="/")}/'
            images = folder_info['images']
            cover_url = f'{folder_url}{quote(images[0], safe="")}' if images else ''
            cover = (f'<img src="{escape(cover_url, quote=True)}" alt="" loading="lazy">'
                     if cover_url else '<div class="cover-placeholder">✳</div>')
            parts.append(f'''<a class="folder-card" href="{escape(folder_url, quote=True)}">
<div class="folder-cover">{cover}</div><div class="folder-body"><div><div class="folder-name">{escape(display_name)}</div>
<div class="folder-meta">{folder_info['image_count']} 张图片</div></div><span class="arrow">↗</span></div></a>''')
        parts.append(FOOTER)
        return ''.join(parts)

    def generate_folder_html(self, folder_info, folder_path):
        """生成文件夹页面HTML"""
        title = folder_path or '根目录'
        parts = [f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f5f6f2"><title>{escape(title)} · Randpic</title>
<style>{GALLERY_STYLE}</style></head><body><div class="shell">
<nav class="topbar"><a class="brand" href="../"><span class="brand-mark">✳</span><span>RANDPIC <span style="color:#8b958e;font-weight:500">/ 图片收藏馆</span></span></a><span class="top-note">{folder_info['image_count']} 张收藏</span></nav>
<div class="crumbs"><a href="../">全部分类</a><span>／</span><span>{escape(title)}</span></div>
<header class="gallery-title"><p class="eyebrow">COLLECTION</p><h1>{escape(title)}</h1><p class="hero-copy">本分类共收藏 {folder_info['image_count']} 张图片。</p></header>''']
        if folder_info['subfolders']:
            parts.append('<nav class="subfolders" aria-label="子文件夹">')
            for subfolder in folder_info['subfolders']:
                subfolder_url = f'./{quote(subfolder, safe="")}/index.html'
                parts.append(f'<a class="subfolder" href="{escape(subfolder_url, quote=True)}">↗　{escape(subfolder)}</a>')
            parts.append('</nav>')
        parts.append('<main class="image-grid">')
        if not folder_info['images']:
            parts.append('<div class="empty">这个分类暂时还没有图片。</div>')
        for image in folder_info['images']:
            image_url = quote(image, safe="")
            safe_url = escape(image_url, quote=True)
            safe_name = escape(image)
            parts.append(f'''<article class="image-card"><a class="image-link" href="{safe_url}" target="_blank" rel="noopener">
<img class="image-preview" src="{safe_url}" alt="{safe_name}" loading="lazy"></a>
<div class="image-info"><span class="image-name" title="{safe_name}">{safe_name}</span><a class="open-link" href="{safe_url}" target="_blank" rel="noopener">查看 ↗</a></div></article>''')
        parts.append(FOOTER)
        return ''.join(parts)

    def generate_static_site(self, no_upload_oss_list):
        """生成静态网站"""
        all_folders = self.get_all_images_with_folders(no_upload_oss_list)
        import shutil
        if self.output_folder.exists():
            shutil.rmtree(self.output_folder)
        # 创建输出文件夹
        self.output_folder.mkdir(parents=True, exist_ok=True)

        # 生成主页
        index_html = self.generate_index_html(all_folders)
        (self.output_folder / 'index.html').write_text(index_html, encoding='utf-8')

        # 为每个文件夹生成页面
        for folder_key, folder_info in all_folders.items():
            if folder_key == '':
                # 根目录的图片页面放在根目录
                folder_dir = self.output_folder
            else:
                # 子文件夹的页面放在对应子文件夹中
                folder_dir = self.output_folder / folder_key
                folder_dir.mkdir(parents=True, exist_ok=True)

            folder_html = self.generate_folder_html(folder_info, folder_key)
            (folder_dir / 'index.html').write_text(folder_html, encoding='utf-8')

        print(f"静态网站已生成到: {self.output_folder}")
        print(f"包含 {len(all_folders)} 个文件夹页面")

        # 复制图片文件
        for root, dirs, files in os.walk(self.source_folder):
            rel_path = os.path.relpath(root, self.source_folder)
            if rel_path == '.':
                target_dir = self.output_folder
            else:
                if rel_path in no_upload_oss_list:
                    continue
                target_dir = self.output_folder / rel_path
                target_dir.mkdir(parents=True, exist_ok=True)

            for file in files:
                if Path(file).suffix.lower() in self.supported_formats:
                    source_file = Path(root) / file
                    target_file = target_dir / file
                    # 复制文件（可以改为硬链接以节省空间）
                    shutil.copy2(source_file, target_file)

    def generate_command_html(self, folder_key: str, file_name: str, no_upload_oss_list):
        if folder_key in no_upload_oss_list:
            return
        all_folders = self.get_all_images_with_folders(no_upload_oss_list)
        folder_info = all_folders[folder_key]
        folder_html = self.generate_folder_html(folder_info, folder_key)

        source_folder2 = self.source_folder / folder_key
        output_folder2 = self.output_folder / folder_key
        if not source_folder2.exists():
            source_folder2.mkdir(parents=True, exist_ok=True)
        if not output_folder2.exists():
            output_folder2.mkdir(parents=True, exist_ok=True)

        folder_dir = self.output_folder / folder_key
        (folder_dir / 'index.html').write_text(folder_html, encoding='utf-8')
        # 生成主页
        index_html = self.generate_index_html(all_folders)
        (self.output_folder / 'index.html').write_text(index_html, encoding='utf-8')

        source_file = self.source_folder / folder_key / file_name
        target_file = self.output_folder / folder_key / file_name
        import shutil
        shutil.copy2(source_file, target_file)

# 测试
if __name__ == "__main__":
    # 源文件夹路径和输出文件夹路径
    source_folder = "C:\\Users\\hu_pa\\Desktop\\randpic"  # 源图片文件夹
    output_folder = "C:\\Users\\hu_pa\\Desktop\\nonebot\\nonebot-plugin-randpic\\static"  # 输出静态网站文件夹
