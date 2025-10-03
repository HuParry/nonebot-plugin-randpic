import zipfile
from pathlib import Path

from PIL import Image
import io

def adaptive_compress_bytes(image_bytes, max_size_mb=1, target_quality=85):
    """
    自适应压缩，确保压缩后的大小不超过限制
    """
    original_size_mb = len(image_bytes) / (1024 * 1024)

    if original_size_mb <= max_size_mb:
        return image_bytes

    print(f"图片大小: {original_size_mb:.2f}MB，超过限制 {max_size_mb}MB，开始自适应压缩...")

    # 打开图片
    image_stream = io.BytesIO(image_bytes)
    with Image.open(image_stream) as img:
        original_size = img.size

        # 如果图片尺寸过大，先进行尺寸调整
        if max(img.size) > 1920:
            scale = 1920 / max(img.size)
            new_size = tuple(int(dim * scale) for dim in img.size)
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            print(f"  - 调整尺寸: {original_size} -> {img.size}")

        # 确保模式兼容
        if img.mode in ('RGBA', 'LA', 'P'):
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')

        # 二分查找合适的质量值
        low, high = 10, 95
        best_quality = target_quality
        best_bytes = None

        while low <= high:
            mid = (low + high) // 2

            # 临时压缩测试
            temp_stream = io.BytesIO()
            img.save(temp_stream, format='JPEG', quality=mid, optimize=True)
            current_bytes = temp_stream.getvalue()
            current_size_mb = len(current_bytes) / (1024 * 1024)

            if current_size_mb <= max_size_mb:
                best_quality = mid
                best_bytes = current_bytes
                # 如果文件大小远小于限制，可以尝试更高质量
                if current_size_mb <= max_size_mb * 0.9:
                    low = mid + 1
                else:
                    break  # 已经接近目标大小
            else:
                high = mid - 1

        if best_bytes is None:
            # 如果找不到合适的质量，使用最高质量压缩
            fallback_stream = io.BytesIO()
            img.save(fallback_stream, format='JPEG', quality=95, optimize=True)
            best_bytes = fallback_stream.getvalue()

        final_size_mb = len(best_bytes) / (1024 * 1024)
        print(f"  - 最终质量: {best_quality}, 大小: {final_size_mb:.2f}MB")

        return best_bytes


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
                    # 计算相对路径，避免包含完整路径
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
    """
        使用示例
        """
    # 读取图片文件为字节流（实际使用时，你的图片流可能来自网络请求、数据库等）
    with open('C:\\Users\\hu_pa\\Desktop\\randpic\\lh\\randpic_lh_2.jpg', 'rb') as f:
        image_bytes = f.read()

    # 自适应压缩（确保不超过限制）
    adaptive_compressed_bytes = compress_image_from_bytes(image_bytes)

    # 保存自适应压缩后的图片
    with open('adaptive_compressed_output.jpg', 'wb') as f:
        f.write(adaptive_compressed_bytes)