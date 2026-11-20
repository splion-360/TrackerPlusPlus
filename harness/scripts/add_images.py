"""Find a product image for every shoe in a persona's web snapshot; save it (and to SingleStore).
PYTHONPATH=. uv run python scripts/add_images.py [persona]"""
import sys, time
from harness import store
from harness.web import images, research
persona = sys.argv[1] if len(sys.argv) > 1 else "jordan"
snap = research.load_snapshot(persona)
t = time.monotonic()
n = images.add_images(snap["candidates"])
research.save_snapshot(snap)
print(f"images for {n}/{len(snap['candidates'])} shoes in {time.monotonic()-t:.1f}s")
for c in snap["candidates"]:
    print(f"- {c['name']}: {(c['imageUrl'] or '—')[:100]}")
print("singlestore:", store.save_candidates(snap))
