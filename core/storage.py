"""
Database-backed file storage.

Why this exists: Render's free web service has an *ephemeral* disk - every
redeploy or restart wipes uploaded files, which is why product photos kept
showing up blank. Here the photos are stored inside the database instead
(which survives redeploys) and served back through the /media/ route.
Photos are also resized (max 1200px) and compressed on upload.
"""
import io
import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000   # refuse decompression bombs (a 40 MP phone photo is fine)


@deconstructible
class DatabaseStorage(Storage):

    def _model(self):
        from core.models import StoredFile  # imported lazily to avoid app-loading issues
        return StoredFile

    def _open(self, name, mode='rb'):
        obj = self._model().objects.get(name=name)
        return ContentFile(bytes(obj.data), name=name)

    def _optimize(self, raw, name):
        """Fix phone-camera rotation, shrink and compress to JPEG.
        Anything that is not a real image is rejected (it used to be stored as-is and
        served back from our own domain, which allowed uploading HTML/JS)."""
        try:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((1200, 1200))
            buffer = io.BytesIO()
            img.save(buffer, 'JPEG', quality=82, optimize=True)
            new_name = os.path.splitext(name)[0] + '.jpg'
            return buffer.getvalue(), 'image/jpeg', new_name
        except Exception as exc:
            raise ValueError(f'Not a valid image: {name}') from exc

    def _save(self, name, content):
        content.seek(0)
        raw = content.read()
        data, content_type, new_name = self._optimize(raw, name)
        if new_name != name:
            new_name = self.get_available_name(new_name)
        self._model().objects.update_or_create(
            name=new_name,
            defaults={'data': data, 'content_type': content_type},
        )
        return new_name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        return len(self._model().objects.get(name=name).data)

    def url(self, name):
        return settings.MEDIA_URL + name.replace('\\', '/')