"""Generate the app icon for Venv to Executable Converter."""
from PIL import Image, ImageDraw, ImageFont
import os

SIZE = 256
img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background rounded rectangle
draw.rounded_rectangle([8, 8, 248, 248], radius=40, fill='#1a1a2e')

# Inner gradient-like layers
draw.rounded_rectangle([20, 20, 236, 236], radius=32, fill='#16213e')
draw.rounded_rectangle([30, 30, 226, 226], radius=28, fill='#0f3460')

# Python-like snake symbol (two interlinked circles)
draw.ellipse([65, 55, 155, 145], fill='#e94560')
draw.ellipse([100, 110, 190, 200], fill='#f5c518')

# Arrow pointing right (compile/export symbol)
arrow_points = [(95, 100), (175, 140), (95, 180)]
draw.polygon(arrow_points, fill='#ffffff')

# Small "EXE" text at bottom
try:
    font = ImageFont.truetype("arial.ttf", 28)
except OSError:
    font = ImageFont.load_default()
draw.text((80, 210), "EXE", fill='#ffffff', font=font, anchor='mt')

# Save as ICO and PNG
script_dir = os.path.dirname(os.path.abspath(__file__))
ico_path = os.path.join(script_dir, 'venv_to_exe.ico')
png_path = os.path.join(script_dir, 'venv_to_exe.png')

img.save(png_path, format='PNG')
img.save(ico_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print(f"Icon saved: {ico_path}")
print(f"PNG saved: {png_path}")
