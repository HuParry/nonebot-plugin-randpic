"""验证、压缩和识别图片。所有入口均在线程中调用，避免阻塞 Bot 事件循环。"""

import hashlib
import io
import warnings
from dataclasses import dataclass
from typing import Optional

import imagehash
from PIL import Image, ImageOps, ImageSequence

from .models import InvalidImage


FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp', 'BMP': '.bmp', 'TIFF': '.tiff'}
MAX_BYTES = 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_ANIMATION_PIXELS = 100_000_000
MAX_FRAMES = 400


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    extension: str
    content_hash: str
    phash: str
    original_hash: str


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    if image.format not in FORMATS:
        image.close()
        raise InvalidImage('不支持这种图片格式，请使用 JPG、PNG、GIF、WebP、BMP 或 TIFF。')
    if image.width * image.height > MAX_PIXELS:
        image.close()
        raise InvalidImage('图片像素过大，请缩小图片后重试。')
    return image


def _rgb(image: Image.Image) -> Image.Image:
    rgba = image.convert('RGBA')
    background = Image.new('RGB', image.size, 'white')
    background.paste(rgba, mask=rgba.getchannel('A'))
    return background


def _static(data: bytes, max_bytes: int, target_quality: int) -> bytes:
    with _open(data) as source:
        image = _rgb(ImageOps.exif_transpose(source))
    image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
    while True:
        best: Optional[bytes] = None
        low, high = 10, max(10, min(95, target_quality))
        while low <= high:
            quality = (low + high) // 2
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=quality, optimize=True)
            candidate = output.getvalue()
            if len(candidate) <= max_bytes:
                best = candidate
                low = quality + 1
            else:
                high = quality - 1
        if best is not None:
            return best
        if image.size == (1, 1):
            raise InvalidImage('图片无法压缩到目标大小。')
        image = image.resize(tuple(max(1, int(size * 0.75)) for size in image.size), Image.Resampling.LANCZOS)


def _animation(data: bytes, max_bytes: int) -> bytes:
    with _open(data) as source:
        frame_count = getattr(source, 'n_frames', 1)
        if frame_count > MAX_FRAMES or frame_count * source.width * source.height > MAX_ANIMATION_PIXELS:
            raise InvalidImage('动画帧数或总像素过大，请先压缩动画后重试。')
        frames, durations = [], []
        loop = source.info.get('loop')
        for frame in ImageSequence.Iterator(source):
            frames.append(frame.convert('RGBA').copy())
            duration = frame.info.get('duration', 100)
            durations.append(max(10, int(duration or 100)))
    # 每次真正尝试减小尺寸；跳帧后使用原帧时长之和，避免加速或拖慢动画。
    scale = min(1.0, 800 / max(frames[0].size))
    for skip in (1, 2, 4, 8, 16):
        for shrink in (1.0, 0.65, 0.4, 0.2):
            size = tuple(max(1, int(side * scale * shrink)) for side in frames[0].size)
            selected = []
            for frame in frames[::skip]:
                rgba = frame.resize(size, Image.Resampling.LANCZOS)
                palette = rgba.quantize(colors=127, method=Image.Quantize.FASTOCTREE)
                transparent = rgba.getchannel('A').point(lambda alpha: 255 if alpha < 128 else 0)
                palette.paste(255, mask=transparent)
                palette.info['transparency'] = 255
                selected.append(palette)
            output = io.BytesIO()
            kwargs = {'loop': int(loop)} if loop is not None else {}
            selected[0].save(
                output, format='GIF', save_all=True, append_images=selected[1:],
                duration=[sum(durations[i:i + skip]) for i in range(0, len(frames), skip)],
                optimize=False, transparency=255, disposal=2, **kwargs,
            )
            if output.tell() <= max_bytes:
                return output.getvalue()
    raise InvalidImage('动画无法压缩到 1MB 以内，请先缩短动画后重试。')


def process_image(data: bytes, max_bytes: int = MAX_BYTES, target_quality: int = 85) -> ProcessedImage:
    if not data:
        raise InvalidImage('图片内容为空。')
    original_hash = hashlib.sha256(data).hexdigest()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with _open(data) as image:
                image.verify()
            with _open(data) as image:
                image.load()
                animated = getattr(image, 'n_frames', 1) > 1 and image.format in {'GIF', 'WEBP', 'PNG'}
                if animated and getattr(image, 'n_frames', 1) > MAX_FRAMES:
                    raise InvalidImage('动画帧数过多，请缩短动画后重试。')
                if animated:
                    if image.n_frames * image.width * image.height > MAX_ANIMATION_PIXELS:
                        raise InvalidImage('动画总像素过大，请先压缩动画后重试。')
                    for frame in ImageSequence.Iterator(image):
                        frame.load()
            if len(data) > max_bytes:
                data = _animation(data, max_bytes) if animated else _static(data, max_bytes, target_quality)
            with _open(data) as image:
                image.load()
                extension = FORMATS[image.format]
                phash = str(imagehash.phash(_rgb(ImageOps.exif_transpose(image))))
    except InvalidImage:
        raise
    except Exception as exc:
        raise InvalidImage('图片损坏、像素过大或格式无法处理。') from exc
    return ProcessedImage(data, extension, hashlib.sha256(data).hexdigest(), phash, original_hash)


# 保留常用图片工具入口，便于既有调用迁移。
def compress_image_from_bytes(image_bytes: bytes, log_progress: bool = True) -> bytes:
    return process_image(image_bytes).data


def adaptive_compress_bytes(image_bytes: bytes, max_size_mb: float = 1, target_quality: int = 85) -> bytes:
    if max_size_mb <= 0:
        raise ValueError('目标大小必须大于 0。')
    return process_image(image_bytes, int(max_size_mb * MAX_BYTES), target_quality).data


def get_image_extension(image_bytes: bytes) -> str:
    try:
        with _open(image_bytes) as image:
            return FORMATS[image.format]
    except Exception as exc:
        raise InvalidImage('无法识别图片格式。') from exc


def compute_phash(image_bytes: bytes) -> str:
    return process_image(image_bytes).phash
