"""Generate branded WhatsApp menu cards from the restaurant's food photos."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).parent
ASSETS = ROOT / "menu-assets"
FONT = Path(r"C:\Windows\Fonts\arial.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\arialbd.ttf")
SIZE = (1080, 1350)

CARDS = [
    ("asun-jollof-rice.jpg", "asun-jollof-card.jpg", "ASUN JOLLOF RICE", 1200),
    ("asun-rice-gizzard.jpg", "asun-gizzard-card.jpg", "ASUN RICE WITH GIZZARD", 3500),
    ("asun-rice-fish.jpg", "asun-fish-card.jpg", "ASUN RICE WITH FISH", 5200),
    ("chicken-chips-4500.jpg", "chicken-chips-card.jpg", "CHICKEN & CHIPS", 4500),
    ("chicken-drumstick-fries.jpg", "drumstick-fries-card.jpg", "CHICKEN DRUMSTICK & FRIES", 3500),
    ("chicken-wings-chips.jpg", "wings-chips-card.jpg", "CHICKEN WINGS & CHIPS", 4500),
    ("gizzard.jpg", "gizzard-1500-card.jpg", "GIZZARD", 1500),
    ("gizzard.jpg", "gizzard-2000-card.jpg", "GIZZARD", 2000),
    ("yam-egg-sauce.jpg", "yam-egg-card.jpg", "YAM & EGG SAUCE", 4000),
]


def font(path, size):
    return ImageFont.truetype(str(path), size)


def centered(draw, text, y, face, fill, canvas_width=1080):
    box = draw.textbbox((0, 0), text, font=face)
    draw.text(((canvas_width - box[2] + box[0]) / 2, y), text, font=face, fill=fill)


def make_card(source_name, output_name, title, price):
    width, height = SIZE
    cream = (255, 249, 235)
    orange = (202, 91, 13)
    deep_orange = (160, 58, 7)
    yellow = (255, 204, 64)
    ink = (74, 42, 21)
    image = Image.new("RGB", SIZE, cream)
    draw = ImageDraw.Draw(image)

    # Restaurant wordmark and a warm, simple title band.
    draw.rectangle((0, 0, width, 188), fill=orange)
    centered(draw, "ADA PLACE & GRILL", 30, font(FONT_BOLD, 42), (255, 255, 255))
    centered(draw, "FRESHLY MADE • FULL OF FLAVOUR", 92, font(FONT, 22), (255, 232, 196))
    draw.rounded_rectangle((438, 145, 642, 154), radius=5, fill=yellow)

    # Preserve each original photo, using a softly blurred fill behind portrait shots.
    photo = Image.open(ASSETS / source_name).convert("RGB")
    frame = (38, 216, 1042, 1000)
    fw, fh = frame[2] - frame[0], frame[3] - frame[1]
    backdrop = ImageOps.fit(photo, (fw, fh), method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(24))
    backdrop = Image.blend(backdrop, Image.new("RGB", backdrop.size, (83, 49, 28)), 0.34)
    image.paste(backdrop, (frame[0], frame[1]))
    photo_fit = ImageOps.contain(photo, (fw - 24, fh - 24), method=Image.Resampling.LANCZOS)
    px, py = (width - photo_fit.width) // 2, frame[1] + (fh - photo_fit.height) // 2
    image.paste(photo_fit, (px, py))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(frame, radius=30, outline=yellow, width=10)

    # Dish name and a high contrast price label.
    draw.rectangle((0, 1020, width, height), fill=cream)
    title_face = font(FONT_BOLD, 48 if len(title) > 22 else 56)
    centered(draw, title, 1040, title_face, ink)
    centered(draw, "PER PORTION", 1112, font(FONT_BOLD, 24), orange)
    draw.rounded_rectangle((225, 1160, 855, 1285), radius=34, fill=yellow, outline=orange, width=5)
    price_text = f"₦{price:,}"
    centered(draw, price_text, 1173, font(FONT_BOLD, 74), deep_orange)
    centered(draw, "ORDER ON WHATSAPP", 1300, font(FONT_BOLD, 18), orange)
    image.save(ASSETS / output_name, "JPEG", quality=90, optimize=True, progressive=True)


if __name__ == "__main__":
    for card in CARDS:
        make_card(*card)
        print(f"Created {card[1]}")
