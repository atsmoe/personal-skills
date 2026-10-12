"""Fit unchanged source photographs inside a white cell-image presentation frame.

Tencent's cell-image API does not expose drawing offsets. This derived layout asset
keeps the original evidence untouched and records both hashes; it is never OCR input.
Pillow is required only for this online presentation adapter.
"""
import math
from pathlib import Path

from bounded_xlsx import file_hash


def framed_photo(source, destination, width, height, padding_x=14, padding_y=8):
    from PIL import Image, ImageOps
    if not all(math.isfinite(v) and 0<v<=8192 for v in (width,height)):
        raise ValueError('Invalid photo host dimensions')
    if not 0<=padding_x<width/2 or not 0<=padding_y<height/2:
        raise ValueError('Photo padding exceeds its host')
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Photo frame must use a new auxiliary output')
    # Three display pixels per pixel keep the preview clear without sending a
    # full camera-sized layout image to a cell that is only a few hundred px wide.
    scale=3
    if round(width*scale)*round(height*scale)>40_000_000:
        raise ValueError('Photo presentation frame exceeds 40 million pixels')
    with Image.open(source) as image:
        if image.width*image.height>40_000_000:
            raise ValueError('Photo exceeds 40 million pixels')
        if image.format not in ('JPEG','PNG','WEBP'):
            raise ValueError('Online photo adapter accepts JPEG, PNG or WEBP')
        picture=ImageOps.exif_transpose(image).convert('RGB')
        picture.thumbnail((int((width-2*padding_x)*scale),int((height-2*padding_y)*scale)),Image.Resampling.LANCZOS)
        canvas=Image.new('RGB',(round(width*scale),round(height*scale)),'white')
        offset=((canvas.width-picture.width)//2,(canvas.height-picture.height)//2)
        canvas.paste(picture,offset)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as stream:
            canvas.save(stream,format='PNG')
    return {'path':str(destination.resolve()),'sha256':file_hash(destination),
            'source_sha256':file_hash(source),'host_width_px':width,'host_height_px':height,
            'padding_x_px':padding_x,'padding_y_px':padding_y,'scale':scale}
