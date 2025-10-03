import os
from pathlib import Path


class StaticImageGalleryGenerator:
    def __init__(self, source_folder, output_folder):
        self.source_folder = Path(source_folder)
        self.output_folder = Path(output_folder)
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg'}

    def get_all_images_with_folders(self):
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
        html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>图片库 - randpic</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
        }
        .folder-container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }
        .folder-card {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: transform 0.3s;
            text-align: center;
            padding: 20px;
        }
        .folder-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }
        .folder-icon {
            font-size: 48px;
            margin-bottom: 15px;
            color: #007bff;
        }
        .folder-name {
            font-weight: bold;
            margin-bottom: 10px;
            word-break: break-all;
        }
        .folder-stats {
            color: #666;
            font-size: 14px;
            margin-bottom: 15px;
        }
        .folder-link {
            display: inline-block;
            padding: 8px 16px;
            background: #007bff;
            color: white;
            text-decoration: none;
            border-radius: 5px;
            transition: background 0.3s;
        }
        .folder-link:hover {
            background: #0056b3;
        }
        .stats {
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }
        .breadcrumb {
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }
        .breadcrumb a {
            color: #007bff;
            text-decoration: none;
        }
        .breadcrumb a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="breadcrumb">
            <a href="./">首页</a>
        </div>
        <h1>图片库 - randpic</h1>
        <div class="stats">
            共 ''' + str(len(all_folders)) + ''' 个文件夹
        </div>
    </div>

    <div class="folder-container">
'''

        for folder_key, folder_info in all_folders.items():
            if folder_key == '':
                display_name = '根目录'
                folder_url = './'
            else:
                display_name = folder_key
                folder_url = f'./{folder_key}/'

            html += f'''
        <div class="folder-card">
            <div class="folder-icon">📁</div>
            <div class="folder-name">{display_name}</div>
            <div class="folder-stats">{folder_info['image_count']} 张图片</div>
            <a href="{folder_url}" class="folder-link">查看图片</a>
        </div>
'''

        html += '''
    </div>
</body>
</html>'''

        return html

    def generate_folder_html(self, folder_info, folder_path):
        """生成文件夹页面HTML"""
        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>图片库 - {folder_path if folder_path else '根目录'}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .breadcrumb {{
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }}
        .breadcrumb a {{
            color: #007bff;
            text-decoration: none;
        }}
        .breadcrumb a:hover {{
            text-decoration: underline;
        }}
        .image-container {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        .image-card {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: transform 0.3s;
            text-align: center;
        }}
        .image-card:hover {{
            transform: scale(1.05);
            z-index: 10;
        }}
        .image-preview {{
            width: 100%;
            height: 150px;
            object-fit: cover;
            display: block;
        }}
        .image-info {{
            padding: 10px;
        }}
        .image-name {{
            font-size: 12px;
            word-break: break-all;
            margin-bottom: 5px;
        }}
        .image-link {{
            display: inline-block;
            padding: 5px 10px;
            background: #28a745;
            color: white;
            text-decoration: none;
            border-radius: 3px;
            font-size: 12px;
        }}
        .image-link:hover {{
            background: #218838;
        }}
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            padding: 10px 20px;
            background: #6c757d;
            color: white;
            text-decoration: none;
            border-radius: 5px;
        }}
        .back-link:hover {{
            background: #5a6268;
        }}
        .stats {{
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }}
        .subfolder-list {{
            margin: 20px 0;
            text-align: center;
        }}
        .subfolder-item {{
            display: inline-block;
            margin: 5px 10px;
            padding: 5px 15px;
            background: #17a2b8;
            color: white;
            text-decoration: none;
            border-radius: 20px;
            font-size: 12px;
        }}
        .subfolder-item:hover {{
            background: #138496;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="breadcrumb">
            <a href="../">首页</a>
            {' / ' + folder_path if folder_path else ''}
        </div>
        <a href="../" class="back-link">← 返回文件夹列表</a>
        <h1>{folder_path if folder_path else '根目录'}</h1>
        <div class="stats">
            共 {folder_info['image_count']} 张图片
        </div>
'''

        if folder_info['subfolders']:
            html += '''
        <div class="subfolder-list">
            <strong>子文件夹:</strong>
'''
            for subfolder in folder_info['subfolders']:
                if folder_path:
                    subfolder_path = f'{folder_path}/{subfolder}'
                    subfolder_url = f'./{subfolder}/index.html'
                else:
                    subfolder_path = subfolder
                    subfolder_url = f'./{subfolder}/index.html'

                html += f'            <a href="{subfolder_url}" class="subfolder-item">{subfolder}</a>\n'

            html += '''        </div>
'''

        html += '''
    </div>

    <div class="image-container">
'''

        for image in folder_info['images']:
            if folder_path:
                image_path = f'{folder_path}/{image}'
            else:
                image_path = image

            html += f'''
        <div class="image-card">
            <img src="{image}" 
                 alt="{image}" 
                 class="image-preview"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='block';">
            <div style="display:none; padding: 20px; text-align: center; color: #666;">
                图片无法显示
            </div>
            <div class="image-info">
                <div class="image-name">{image}</div>
                <a href="{image}" 
                   target="_blank" 
                   class="image-link">查看</a>
            </div>
        </div>
'''

        html += '''
    </div>
</body>
</html>'''

        return html

    def generate_static_site(self):
        """生成静态网站"""
        all_folders = self.get_all_images_with_folders()
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
                target_dir = self.output_folder / rel_path
                target_dir.mkdir(parents=True, exist_ok=True)

            for file in files:
                if Path(file).suffix.lower() in self.supported_formats:
                    source_file = Path(root) / file
                    target_file = target_dir / file
                    # 复制文件（可以改为硬链接以节省空间）
                    shutil.copy2(source_file, target_file)

    def generate_command_html(self, folder_key: str, file_name: str):
        all_folders = self.get_all_images_with_folders()
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

    generator = StaticImageGalleryGenerator(source_folder, output_folder)
    generator.generate_static_site()