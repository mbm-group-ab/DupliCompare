"""One-off script to generate the DupliCompare icon (PNG + ICO)."""
from PIL import Image, ImageDraw

SIZE = 512
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

bg = (79, 70, 229, 255)      # indigo
accent = (20, 184, 166, 255)  # teal
white = (255, 255, 255, 255)

# rounded square background
d.rounded_rectangle([16, 16, SIZE - 16, SIZE - 16], radius=110, fill=bg)

# back document (teal), offset top-right
back = [SIZE * 0.34, SIZE * 0.16, SIZE * 0.78, SIZE * 0.62]
d.rounded_rectangle(back, radius=28, fill=accent)

# front document (white), offset bottom-left, overlapping
front = [SIZE * 0.20, SIZE * 0.34, SIZE * 0.64, SIZE * 0.80]
d.rounded_rectangle(front, radius=28, fill=white)

# lines on front doc to suggest a file
lx0, lx1 = SIZE * 0.28, SIZE * 0.56
for i, ly in enumerate([0.45, 0.53, 0.61, 0.69]):
    w = lx1 if i < 3 else lx1 - SIZE * 0.08
    d.line([(lx0, SIZE * ly), (w, SIZE * ly)], fill=(79, 70, 229, 140), width=6)

# small "duplicate" badge circle bottom-right with a check
badge_c = (SIZE * 0.80, SIZE * 0.80)
r = SIZE * 0.12
d.ellipse([badge_c[0] - r, badge_c[1] - r, badge_c[0] + r, badge_c[1] + r], fill=accent, outline=bg, width=6)
d.line(
    [
        (badge_c[0] - r * 0.45, badge_c[1]),
        (badge_c[0] - r * 0.1, badge_c[1] + r * 0.35),
        (badge_c[0] + r * 0.5, badge_c[1] - r * 0.4),
    ],
    fill=white, width=10, joint="curve",
)

img.save("assets/icon.png")
img.resize((256, 256)).save("assets/icon-256.png")
img.save(
    "assets/icon.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("done")
