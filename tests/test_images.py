import io
import unittest

from PIL import Image

from tests.support import picture
from nonebot_plugin_randpic.compress import process_image
from nonebot_plugin_randpic.models import InvalidImage


class ImageTests(unittest.TestCase):
    def test_small_images_are_validated_and_kept_byte_identical(self):
        for format, extension in [('JPEG', '.jpg'), ('PNG', '.png'), ('GIF', '.gif'), ('WEBP', '.webp'), ('BMP', '.bmp'), ('TIFF', '.tiff')]:
            data = picture(format=format)
            result = process_image(data)
            self.assertEqual(result.data, data)
            self.assertEqual(result.extension, extension)
            self.assertEqual(len(result.phash), 16)
        for data in (b'', b'not a picture', picture()[:30]):
            with self.assertRaises(InvalidImage):
                process_image(data)

    def test_large_noise_is_compressed_and_thin_images_do_not_reach_zero_size(self):
        data = picture(size=(1000, 1000))
        self.assertGreater(len(data), 1024 * 1024)
        result = process_image(data)
        self.assertLessEqual(len(result.data), 1024 * 1024)
        self.assertEqual(result.extension, '.jpg')
        result = process_image(picture(size=(1, 8000)), max_bytes=1024)
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertGreaterEqual(image.width, 1)
            self.assertGreaterEqual(image.height, 1)

    def test_animated_compression_retains_timing_and_play_once(self):
        frames = [Image.open(io.BytesIO(picture(seed, size=(128, 128)))).convert('RGB') for seed in range(1, 9)]
        original = io.BytesIO()
        durations = [100 + 10 * n for n in range(len(frames))]
        frames[0].save(original, format='GIF', save_all=True, append_images=frames[1:], duration=durations)
        data = original.getvalue()
        result = process_image(data, max_bytes=18000)
        self.assertLessEqual(len(result.data), 18000)
        with Image.open(io.BytesIO(result.data)) as animation:
            self.assertNotIn('loop', animation.info)
            self.assertGreater(animation.n_frames, 1)
            total = 0
            for index in range(animation.n_frames):
                animation.seek(index)
                total += animation.info['duration']
            self.assertEqual(total, sum(durations))

    def test_transparent_palette_image_can_be_compressed(self):
        source = Image.open(io.BytesIO(picture(size=(400, 400)))).quantize(256)
        source.info['transparency'] = 0
        output = io.BytesIO()
        source.save(output, format='PNG')
        result = process_image(output.getvalue(), max_bytes=10000)
        self.assertLessEqual(len(result.data), 10000)
        self.assertEqual(result.extension, '.jpg')
