# Jewelix AI Studio

Prototype of the Jewelix AI Studio plugin. Runs entirely in the browser; there is no backend yet.

- **Enhance**: removes the background (automatic, with an in-browser AI cutout for difficult photos) and shows the piece on white, with yellow / rose / white gold previews
- **Show on Model**: creates a realistic photo of the piece being worn, using the open Qwen-Image-Edit model on a free Hugging Face Space. Model photos live in `models/` (Maya, Meera); rings and bracelets use stock hand/wrist photos
- **Blend**: combines two designs into a new one
- **Inventory Match**: searches stock from a client's reference photo (shows a "connect inventory" state until the ERP is linked)
- **Raw / Catalog**: original uploads for the session, and finished results saved with "Add to Catalog" (kept in the browser)
- **Choose from inventory**: demo stock pieces with purity, weight, carat and stone details

## Show on Model limits

The free Hugging Face service allows a limited amount of GPU time per day (roughly 1 photo without an account, about 5–10 with a free account token). Add a free **Read** token from huggingface.co/settings/tokens in *Model Options → Free Hugging Face token*. It is stored only in that browser and is never part of the code.

## Run locally

```bash
python3 -m http.server 5173
```

Then open http://localhost:5173.

`visualizer-v1.html` is the earlier single-tool version.
