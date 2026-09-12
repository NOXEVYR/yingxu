"""Build native icons matching the application's viewfinder/play favicon.

Uses the project's existing Pillow build dependency, with no network activity.
The native icon shape is the same four open corners used by the UI brand; it
must not revert to the former filmstrip icon when native launchers are rebuilt.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
BACKGROUND = "#202327"
FOREGROUND = "#ffffff"
VIEWBOX_SIZE = 64
CORNER_PATH = "M16 22v-6h12m8 0h12v12m0 8v12H36m-8 0H16V36"
PLAY_PATH = "m27 25 12 7-12 7z"
STROKE_WIDTH = 4
CORNER_RADIUS = 16
# Explicit filled polygons reproduce the SVG's 4px butt caps and miter joins.
# Their separation is intentional: this is an open viewfinder, not a film frame.
CORNER_POLYGONS = (
    ((14,22),(14,14),(28,14),(28,18),(18,18),(18,22)),
    ((36,14),(50,14),(50,28),(46,28),(46,18),(36,18)),
    ((50,36),(50,50),(36,50),(36,46),(46,46),(46,36)),
    ((28,50),(14,50),(14,36),(18,36),(18,46),(28,46)),
)
PLAY_POINTS = ((27,25),(39,32),(27,39))
WINDOWS_SIZES = [(size, size) for size in (16, 24, 32, 48, 64, 128, 256)]


def render_mark():
    """Render the 64-unit favicon geometry at 1024px before downsampling."""
    scale = 16
    image = Image.new("RGBA", (VIEWBOX_SIZE * scale, VIEWBOX_SIZE * scale), (0,0,0,0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0,0,image.width-1,image.height-1),
                           radius=CORNER_RADIUS * scale,fill=BACKGROUND)
    for polygon in (*CORNER_POLYGONS, PLAY_POINTS):
        draw.polygon([(x * scale,y * scale) for x,y in polygon],fill=FOREGROUND)
    return image


def generate(output=ROOT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEWBOX_SIZE} {VIEWBOX_SIZE}">
<rect width="{VIEWBOX_SIZE}" height="{VIEWBOX_SIZE}" rx="{CORNER_RADIUS}" fill="{BACKGROUND}"/>
<path d="{CORNER_PATH}" stroke="{FOREGROUND}" stroke-width="{STROKE_WIDTH}" fill="none"/>
<path d="{PLAY_PATH}" fill="{FOREGROUND}"/>
</svg>'''
    (output / "brand.svg").write_text(svg, encoding="utf-8")
    image = render_mark()
    # WinForms/.NET Framework must be able to decode every frame repeatedly.
    # Use uncompressed 32-bit DIB entries instead of PNG-compressed ICO frames;
    # the existing 7 sizes avoid OS scaling at common desktop/tray DPI values.
    image.resize((256,256),Image.Resampling.LANCZOS).save(
        output / "brand.ico",sizes=WINDOWS_SIZES,bitmap_format="bmp")
    image.save(output / "brand.icns")


if __name__ == "__main__":
    generate()
    print("Created matching viewfinder/play icons: SVG, Windows ICO, macOS ICNS")
