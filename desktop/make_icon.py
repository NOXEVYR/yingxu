"""Render the original film/play mark into offline Windows and macOS icons.

Pillow is a build tool already used by this project. Icon files are generated
before packaging; this module performs no work or downloads on import.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
BACKGROUND = "#202020"
FRAME = "#f5f5f5"
SCREEN = "#292929"
PLAY = "#ffffff"
WINDOWS_SIZES = [(size, size) for size in (16, 24, 32, 48, 64, 128, 256)]


def render_mark():
    """Keep the original geometry, rendered at 4x for small-icon antialiasing."""
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def rect(box, radius, color):
        draw.rounded_rectangle(tuple(int(x * scale) for x in box), radius * scale, fill=color)

    rect((0, 0, 255, 255), 58, BACKGROUND)
    rect((44, 53, 212, 203), 18, FRAME)
    rect((66, 69, 190, 187), 7, SCREEN)
    draw.polygon([(108 * scale, 94 * scale), (156 * scale, 128 * scale),
                  (108 * scale, 162 * scale)], fill=PLAY)
    for x, y in [(51, 73), (51, 111), (51, 150), (196, 90), (196, 128), (196, 166)]:
        rect((x, y, x + 9, y + 17), 3, BACKGROUND)
    return image


def generate(output=ROOT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<rect width="256" height="256" rx="58" fill="{BACKGROUND}"/>
<rect x="44" y="53" width="168" height="150" rx="18" fill="{FRAME}"/>
<rect x="66" y="69" width="124" height="118" rx="7" fill="{SCREEN}"/>
<path d="M108 94L156 128L108 162Z" fill="{PLAY}"/>
<g fill="{BACKGROUND}"><rect x="51" y="73" width="9" height="17" rx="3"/><rect x="51" y="111" width="9" height="17" rx="3"/><rect x="51" y="150" width="9" height="17" rx="3"/><rect x="196" y="90" width="9" height="17" rx="3"/><rect x="196" y="128" width="9" height="17" rx="3"/><rect x="196" y="166" width="9" height="17" rx="3"/></g>
</svg>'''
    (output / "brand.svg").write_text(svg, encoding="utf-8")
    image = render_mark()
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        output / "brand.ico", sizes=WINDOWS_SIZES)
    image.save(output / "brand.icns")


if __name__ == "__main__":
    generate()
    print("Created original monochrome film icons: SVG, Windows ICO, macOS ICNS")
