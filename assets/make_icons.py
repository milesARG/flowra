"""Build every Flowra icon from the master logo image (assets/logo_master.png).

The master's mark is used as-is (no retouching). Its background is a semi-transparent dark
halo designed for dark surfaces, so icons place the crop on the logo's own navy-black
(#050B18, sampled from the master) inside a rounded square.
Outputs:
  assets/logo.png        512x512  tray icon + settings window sidebar/favicon
  assets/flowra.ico      16-256   Flowra.exe + installer icon
  assets/logo_full.png   original master, unmodified
  assets/preview_sizes.png        check strip of the icon at 16/32/48/128 px
"""
import shutil
from pathlib import Path
from PIL import Image, ImageDraw

HERE = Path(__file__).parent
MASTER = HERE / "logo_master.png"
CENTER = (620, 622)      # centre of the mark + its glow in the master (measured)
SIDE = 640               # square crop: mark fills ~80%
SIDE_SMALL = 560         # tighter crop for 16/32 px so the bars stay legible


def crop(side: int) -> Image.Image:
    im = Image.open(MASTER).convert("RGBA")
    cx, cy = CENTER
    return im.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2))


BG = (5, 11, 24, 255)     # sampled opaque background colour of the master
RADIUS = 0.22            # rounded-square corner radius, fraction of the side


def icon(size: int) -> Image.Image:
    c = crop(SIDE_SMALL if size <= 32 else SIDE)
    tile = Image.new("RGBA", c.size, BG)
    tile.alpha_composite(c)
    mask = Image.new("L", c.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, c.width - 1, c.height - 1], radius=int(c.width * RADIUS), fill=255)
    tile.putalpha(mask)
    return tile.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    icon(512).save(HERE / "logo.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big = icon(256)
    big.save(HERE / "flowra.ico", sizes=[(s, s) for s in sizes],
             append_images=[icon(s) for s in sizes if s != 256])
    shutil.copyfile(MASTER, HERE / "logo_full.png")
    strip = Image.new("RGBA", (16 + 32 + 48 + 128 + 50, 128), (235, 235, 235, 255))
    x = 10
    for s in (16, 32, 48, 128):
        im = icon(s); strip.paste(im, (x, (128 - s) // 2), im); x += s + 10
    strip.save(HERE / "preview_sizes.png")
    print("icons written")
