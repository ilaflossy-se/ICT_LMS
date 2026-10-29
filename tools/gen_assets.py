"""Generates own-work media for the LMS prototype (no third-party material):
 - 5 abstract course covers (PNG 1280x720 = naive version, WebP 320/640 = optimised)
 - notification chime (WAV -> MP3 via ffmpeg)
Run: python3 -I gen_assets.py <before_media_dir> <after_media_dir>"""
import sys, math, struct, wave
from PIL import Image, ImageDraw

before, after = sys.argv[1], sys.argv[2]
PALETTES = [((39,64,107),(88,140,200)), ((30,90,90),(110,190,170)),
            ((90,50,110),(190,140,210)), ((110,70,40),(220,170,110)),
            ((40,40,60),(120,130,170))]

def cover(i):
    W, H = 1280, 720
    c1, c2 = PALETTES[i]
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):                      # diagonal gradient
        for x in range(0, W):
            t = (x / W * 0.6 + y / H * 0.4)
            px[x, y] = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
    d = ImageDraw.Draw(img, "RGBA")
    for k in range(6):                      # simple geometric shapes, different for each cover
        r = 90 + 55 * ((i + k) % 5)
        cx = (180 + 210 * k + 97 * i) % W
        cy = (120 + 130 * k + 61 * i) % H
        if (i + k) % 2:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 38))
        else:
            d.rectangle([cx - r, cy - r // 2, cx + r, cy + r // 2], outline=(255, 255, 255, 90), width=6)
    return img

for i in range(5):
    im = cover(i)
    im.save(f"{before}/cover-{i}.png", optimize=False)            # naive export (before)
    for w in (320, 640):                                          # optimised (after)
        h = w * 9 // 16
        im.resize((w, h), Image.LANCZOS).save(f"{after}/cover-{i}-{w}.webp", "WEBP", quality=80, method=6)

# notification chime: two short sine tones with fade, 16-bit mono 22.05 kHz
rate = 22050
def tone(freq, dur):
    n = int(rate * dur)
    return [int(32767 * 0.5 * math.sin(2 * math.pi * freq * t / rate) *
                min(1, t / 200, (n - t) / (n * 0.6))) for t in range(n)]
samples = tone(880, 0.12) + tone(1318, 0.22)
with wave.open("notify.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
