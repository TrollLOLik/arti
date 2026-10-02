"""Image regions use normalized coordinates of the original displayed image."""
from dataclasses import dataclass
from io import BytesIO
import math
from PIL import Image,ImageOps
from materials.types import Locator,MaterialError


@dataclass(frozen=True)
class RegionRequest:
    bbox: tuple[float,float,float,float]
    scale: float=1
    def __post_init__(self):
        Locator('region',bbox=self.bbox)
        if not math.isfinite(self.scale) or not 1<=self.scale<=4: raise MaterialError('region_scale_budget')


def displayed_image(data):
    with Image.open(BytesIO(data)) as original:
        image=ImageOps.exif_transpose(original).convert('RGB')
    if image.width*image.height>16_000_000:
        image.close(); raise MaterialError('region_pixel_budget')
    return image


def crop(data,request):
    with displayed_image(data) as image:
        x0,y0,x1,y1=request.bbox
        pixels=(math.floor(x0*image.width),math.floor(y0*image.height),math.ceil(x1*image.width),math.ceil(y1*image.height))
        region=image.crop(pixels)
        if region.width*region.height*request.scale**2>8_000_000:
            region.close(); raise MaterialError('region_pixel_budget')
        if request.scale!=1:
            resized=region.resize((round(region.width*request.scale),round(region.height*request.scale)),Image.Resampling.LANCZOS)
            region.close(); region=resized
        return region,dict(original_display_size=list(image.size),pixel_box=list(pixels),scale=request.scale,
            coordinate_space='exif_displayed_original_normalized')


def map_box(box,outer):
    x0,y0,x1,y1=outer
    return (x0+box[0]*(x1-x0),y0+box[1]*(y1-y0),x0+box[2]*(x1-x0),y0+box[3]*(y1-y0))


def needs_refinement(*,significance,uncertainty,already_reviewed=False):
    """A labelled policy score, never a probability of a correct observation."""
    if not all(math.isfinite(v) and 0<=v<=1 for v in (significance,uncertainty)): raise MaterialError('invalid_review_priority')
    return not already_reviewed and significance*uncertainty>=.3
