import zipfile
from pathlib import Path
from nonebot.log import logger
from PIL import Image
import io


def _compress_gif_bytes(image_bytes, max_size_mb=1):
    """压缩动态GIF，保留动画效果。按帧跳过 → 缩放 → 减色的顺序逐步加大压缩力度。"""
    image_stream = io.BytesIO(image_bytes)
    img = Image.open(image_stream)
    loop = img.info.get('loop', 0)

    frames, durations = [], []
    try:
        while True:
            frames.append(img.copy().convert('RGBA'))
            durations.append(img.info.get('duration', 100))
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    original_size = frames[0].size
    best_bytes = None

    for frame_skip in [1, 2, 3, 4]:
        for scale in [1.0, 0.75, 0.5]:
            for n_colors in [256, 128, 64]:
                sel_frames = frames[::frame_skip]
                # 跳帧后将帧时长乘以 frame_skip，保持动画整体播放速度不变
                sel_durations = [d * frame_skip for d in durations[::frame_skip]]

                if scale < 1.0:
                    new_size = tuple(int(d * scale) for d in original_size)
                    sel_frames = [f.resize(new_size, Image.Resampling.LANCZOS) for f in sel_frames]

                quantized = [
                    f.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
                    for f in sel_frames
                ]

                out = io.BytesIO()
                quantized[0].save(
                    out, format='GIF', save_all=True,
                    append_images=quantized[1:],
                    loop=loop, duration=sel_durations, optimize=True
                )

                result = out.getvalue()
                result_mb = len(result) / (1024 * 1024)

                if best_bytes is None or len(result) < len(best_bytes):
                    best_bytes = result

                if result_mb <= max_size_mb:
                    logger.info(
                        f"GIF压缩完成: 帧跳过={frame_skip}, 缩放={scale:.2f}, "
                        f"颜色={n_colors}, 大小={result_mb:.2f}MB"
                    )
                    return result

    logger.warning(
        f"GIF无法压缩到 {max_size_mb}MB 以内，"
        f"返回最小版本 ({len(best_bytes) / (1024 * 1024):.2f}MB)"
    )
    return best_bytes


def _compress_static_bytes(image_bytes, max_size_mb=1, target_quality=85):
    """压缩静态图片（JPEG/PNG 等），转为 JPEG 输出。"""
    image_stream = io.BytesIO(image_bytes)
    with Image.open(image_stream) as img:
        original_size = img.size

        if max(img.size) > 1920:
            scale = 1920 / max(img.size)
            new_size = tuple(int(dim * scale) for dim in img.size)
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            logger.info(f"  - 调整尺寸: {original_size} -> {img.size}")

        if img.mode in ('RGBA', 'LA', 'P'):
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')

        low, high = 10, 95
        best_quality = target_quality
        best_bytes = None

        while low <= high:
            mid = (low + high) // 2

            temp_stream = io.BytesIO()
            img.save(temp_stream, format='JPEG', quality=mid, optimize=True)
            current_bytes = temp_stream.getvalue()
            current_size_mb = len(current_bytes) / (1024 * 1024)

            if current_size_mb <= max_size_mb:
                best_quality = mid
                best_bytes = current_bytes
                if current_size_mb <= max_size_mb * 0.9:
                    low = mid + 1
                else:
                    break
            else:
                high = mid - 1

        if best_bytes is None:
            fallback_stream = io.BytesIO()
            img.save(fallback_stream, format='JPEG', quality=95, optimize=True)
            best_bytes = fallback_stream.getvalue()

        final_size_mb = len(best_bytes) / (1024 * 1024)
        logger.info(f"  - 最终质量: {best_quality}, 大小: {final_size_mb:.2f}MB")

        return best_bytes


def adaptive_compress_bytes(image_bytes, max_size_mb=1, target_quality=85):
    """
    自适应压缩，确保压缩后的大小不超过限制。
    动态 GIF 保持 GIF 格式输出，静态图片转为 JPEG 输出。
    """
    original_size_mb = len(image_bytes) / (1024 * 1024)

    if original_size_mb <= max_size_mb:
        return image_bytes

    logger.info(f"图片大小: {original_size_mb:.2f}MB，超过限制 {max_size_mb}MB，开始自适应压缩...")

    image_stream = io.BytesIO(image_bytes)
    with Image.open(image_stream) as img:
        fmt = img.format
        is_animated = getattr(img, 'n_frames', 1) > 1

    if fmt == 'GIF' and is_animated:
        return _compress_gif_bytes(image_bytes, max_size_mb)

    return _compress_static_bytes(image_bytes, max_size_mb, target_quality)


def compress_image_from_bytes(image_bytes):
    """
    严格控制文件大小的压缩函数
    参数: image_bytes - 图片字节流 (bytes类型)
    返回: 压缩后的图片字节流 (bytes类型)
    """
    return adaptive_compress_bytes(
        image_bytes=image_bytes,
        max_size_mb=1,
        target_quality=85
    )


def get_image_extension(image_bytes):
    """根据压缩后的字节内容返回正确的文件扩展名，避免依赖来源 URL 的扩展名"""
    with Image.open(io.BytesIO(image_bytes)) as img:
        fmt = img.format
        is_animated = getattr(img, 'n_frames', 1) > 1
    if fmt == 'GIF' and is_animated:
        return '.gif'
    return '.jpg'


def compress_folder_basic(folder_path, zip_path, include_subfolders=True):
    """
    将文件夹压缩成zip文件

    参数:
    - folder_path: 要压缩的文件夹路径
    - zip_path: 输出的zip文件路径
    - include_subfolders: 是否包含子文件夹
    """
    folder = Path(folder_path)

    if not folder.exists():
        print(f"文件夹不存在: {folder_path}")
        return False

    if not folder.is_dir():
        print(f"路径不是文件夹: {folder_path}")
        return False

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in folder.rglob("*") if include_subfolders else folder.iterdir():
                if file_path.is_file():
                    relative_path = file_path.relative_to(folder)
                    zipf.write(file_path, relative_path)
                    print(f"添加文件: {relative_path}")

        print(f"压缩完成: {zip_path}")
        return True
    except Exception as e:
        print(f"压缩失败: {e}")
        return False


# 测试
if __name__ == '__main__':
    with open('C:\\Users\\hu_pa\\Desktop\\randpic\\lh\\randpic_lh_2.jpg', 'rb') as f:
        image_bytes = f.read()

    adaptive_compressed_bytes = compress_image_from_bytes(image_bytes)

    with open('adaptive_compressed_output.jpg', 'wb') as f:
        f.write(adaptive_compressed_bytes)
