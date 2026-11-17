"""Generate Ada Place & Grill's black-and-orange WhatsApp menu artwork."""
import json
import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).parent
ASSETS = ROOT / "menu-assets"
FONT = Path(r"C:\Windows\Fonts\arial.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\arialbd.ttf")
MENU = json.loads((ROOT / "menu.json").read_text(encoding="utf-8"))
BLACK = (9, 9, 9)
PANEL = (21, 21, 21)
ORANGE = (255, 112, 0)
WHITE = (255, 255, 255)
MUTED = (205, 205, 205)

PRICE_CARDS = [
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

PHOTO_OVERRIDES = {
    "asun-jollof-card.jpg": "asun-jollof-rice.jpg",
    "asun-gizzard-card.jpg": "asun-rice-gizzard.jpg",
    "asun-fish-card.jpg": "asun-rice-fish.jpg",
    "chicken-chips-card.jpg": "chicken-chips-4500.jpg",
    "drumstick-fries-card.jpg": "chicken-drumstick-fries.jpg",
    "wings-chips-card.jpg": "chicken-wings-chips.jpg",
    "gizzard-1500-card.jpg": "gizzard.jpg",
    "gizzard-2000-card.jpg": "gizzard.jpg",
    "yam-egg-card.jpg": "yam-egg-sauce.jpg",
}


def font(size, bold=False):
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT), size)


def centered(draw, text, y, face, fill, width):
    box = draw.textbbox((0, 0), text, font=face)
    draw.text(((width - (box[2] - box[0])) / 2, y), text, font=face, fill=fill)


def make_price_card(source_name, output_name, title, price):
    width, height = 1080, 1350
    image = Image.new("RGB", (width, height), BLACK)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 188), fill=ORANGE)
    centered(draw, "ADA PLACE & GRILL", 30, font(42, True), BLACK, width)
    centered(draw, "FRESHLY MADE • FULL OF FLAVOUR", 92, font(22), BLACK, width)
    draw.rectangle((438, 145, 642, 154), fill=BLACK)

    photo = Image.open(ASSETS / source_name).convert("RGB")
    frame = (38, 216, 1042, 1000)
    fw, fh = frame[2] - frame[0], frame[3] - frame[1]
    backdrop = ImageOps.fit(photo, (fw, fh), method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(24))
    backdrop = Image.blend(backdrop, Image.new("RGB", backdrop.size, BLACK), 0.40)
    image.paste(backdrop, (frame[0], frame[1]))
    photo_fit = ImageOps.contain(photo, (fw - 24, fh - 24), method=Image.Resampling.LANCZOS)
    px, py = (width - photo_fit.width) // 2, frame[1] + (fh - photo_fit.height) // 2
    image.paste(photo_fit, (px, py))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(frame, radius=30, outline=ORANGE, width=10)
    centered(draw, title, 1040, font(48 if len(title) > 22 else 56, True), WHITE, width)
    centered(draw, "PER PORTION", 1112, font(24, True), ORANGE, width)
    draw.rounded_rectangle((225, 1160, 855, 1285), radius=34, fill=ORANGE)
    centered(draw, f"₦{price:,}", 1173, font(74, True), BLACK, width)
    centered(draw, "ORDER ON WHATSAPP", 1300, font(18, True), MUTED, width)
    image.save(ASSETS / output_name, "JPEG", quality=90, optimize=True, progressive=True)


def display_name(name):
    return re.sub(r"\s*\([^)]*(?:portion|final price confirmed by staff)[^)]*\)\s*$", "", name, flags=re.I)


def display_price(name, price):
    if price is not None:
        return f"₦{price:,}"
    match = re.search(r"₦\s*([\d,]+)\s*[–-]\s*₦\s*([\d,]+)", name)
    return f"₦{match.group(1)}–₦{match.group(2)}" if match else "Price confirmed by staff"


def photo_for(item):
    if len(item) < 3 or not item[2]:
        return None
    filename = PHOTO_OVERRIDES.get(item[2], item[2])
    path = ASSETS / filename
    if not path.is_file():
        return None
    image = Image.open(path).convert("RGB")
    # Extract only the actual dish photo from the existing square rice/pasta/grill cards.
    if filename in {"ada-jollof-card.png", "ada-fried-card.png", "ada-pasta-card.png", "ada-grill-card.png"}:
        image = image.crop((32, 188, image.width - 32, min(808, image.height)))
    elif filename.lower().endswith(".png") and image.size == (1080, 1080):
        # Older drink cards have the bottle/product photo in the center panel.
        image = image.crop((267, 190, 813, 737))
    return image


def wrap_lines(draw, text, face, max_width, max_lines=3):
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=face)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:max_lines]


def make_catalogue(category, page_number, items):
    width = 1080
    rows = (len(items) + 1) // 2
    card_h = 326
    gap_y = 18
    height = 176 + rows * card_h + (rows - 1) * gap_y + 24
    image = Image.new("RGB", (width, height), BLACK)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 154), fill=ORANGE)
    centered(draw, f"ADA PLACE & GRILL  •  {category.upper()}", 30, font(40, True), BLACK, width)
    centered(draw, f"PAGE {page_number}  •  CHOOSE AN ITEM FROM THE MESSAGE", 94, font(20, True), BLACK, width)

    margin, gap_x = 38, 22
    card_w = (width - margin * 2 - gap_x) // 2
    first_y = 176
    for index, item in enumerate(items):
        col, row = index % 2, index // 2
        x = margin + col * (card_w + gap_x)
        y = first_y + row * (card_h + gap_y)
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=24, fill=PANEL, outline=ORANGE, width=4)
        draw.ellipse((x + 16, y + 14, x + 66, y + 64), fill=ORANGE)
        draw.text((x + 33, y + 23), str(index + 1), font=font(27, True), fill=BLACK)

        photo = photo_for(item)
        box = (x + 16, y + 76, x + 198, y + 304)
        draw.rounded_rectangle(box, radius=16, fill=(37, 37, 37))
        if photo:
            fit = ImageOps.contain(photo, (box[2] - box[0] - 10, box[3] - box[1] - 10), method=Image.Resampling.LANCZOS)
            image.paste(fit, (box[0] + (box[2] - box[0] - fit.width) // 2, box[1] + (box[3] - box[1] - fit.height) // 2))
        else:
            initials = "".join(word[0] for word in display_name(item[0]).split()[:2]).upper()
            draw.ellipse((x + 54, y + 134, x + 162, y + 242), outline=ORANGE, width=5)
            draw.text((x + 108, y + 188), initials, font=font(38, True), fill=ORANGE, anchor="mm")

        text_x, text_w = x + 215, card_w - 233
        name_face = font(27 if len(display_name(item[0])) < 24 else 23, True)
        lines = wrap_lines(draw, display_name(item[0]), name_face, text_w, 4)
        text_y = y + 90
        for line in lines:
            draw.text((text_x, text_y), line, font=name_face, fill=WHITE)
            text_y += 35
        price = display_price(item[0], item[1])
        draw.text((text_x, min(text_y + 10, y + 246)), price, font=font(25, True), fill=ORANGE)
        unit = "PER ITEM" if category == "Drinks" else ("PER PACK" if "per pack" in item[0].lower() else "PER PORTION")
        draw.text((text_x, min(text_y + 48, y + 283)), unit, font=font(16, True), fill=MUTED)

    slug = category.casefold().replace(" & ", "-").replace(" ", "-")
    image.save(ASSETS / f"catalogue-{slug}-{page_number}.jpg", "JPEG", quality=88, optimize=True, progressive=True)


def make_category_menu():
    width, height = 1080, 1500
    image = Image.new("RGB", (width, height), BLACK)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 220), fill=ORANGE)
    centered(draw, "ADA PLACE & GRILL", 43, font(54, True), BLACK, width)
    centered(draw, "GOOD FOOD. GREAT TASTE.", 124, font(27, True), BLACK, width)
    centered(draw, "WHAT ARE YOU CRAVING?", 255, font(38, True), WHITE, width)
    centered(draw, "Choose a menu category", 311, font(26), ORANGE, width)
    row_y, row_h, gap = 370, 110, 14
    for index, name in enumerate(MENU, 1):
        y = row_y + (index - 1) * (row_h + gap)
        draw.rounded_rectangle((82, y, 998, y + row_h), radius=27, fill=PANEL, outline=ORANGE, width=4)
        draw.ellipse((104, y + 16, 182, y + 94), fill=ORANGE)
        draw.text((143, y + 55), str(index), font=font(39, True), fill=BLACK, anchor="mm")
        draw.text((214, y + 31), name, font=font(36, True), fill=WHITE)
    centered(draw, "Reply with the number of your choice", 1402, font(26, True), WHITE, width)
    centered(draw, "Example: reply 1 for Rice", 1444, font(23), ORANGE, width)
    image.save(ASSETS / "category-menu.png", "PNG", optimize=True)


if __name__ == "__main__":
    for card in PRICE_CARDS:
        make_price_card(*card)
    for category, all_items in MENU.items():
        for page_start in range(0, len(all_items), 8):
            page_number = page_start // 8 + 1
            make_catalogue(category, page_number, all_items[page_start:page_start + 8])
    make_category_menu()
    print("Created 9 price cards, 11 category catalogues, and the category menu in black and orange.")
