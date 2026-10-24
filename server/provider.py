"""Image-generation provider used by Show on Model / Show on Mannequin.

Current route: Hugging Face Space `prithivMLmods/Qwen-Image-Edit-2511-LoRAs-Fast` (model Qwen-Image-Edit-2511),
called through the Gradio API. It runs on Hugging Face ZeroGPU, so usage is billed in GPU time against the
calling account's ZeroGPU quota, not per image. Each call is recorded with its measured duration so costs can be
estimated (and reconciled later against the Hugging Face bill).
"""
import os
import time

SPACE = os.environ.get("JEWELREX_AI_SPACE", "prithivMLmods/Qwen-Image-Edit-2511-LoRAs-Fast")
ENDPOINT = "/edit_image"
PROVIDER = "huggingface-zerogpu"


class ProviderError(Exception):
    def __init__(self, message, call, quota=False):
        super().__init__(message)
        self.call, self.quota = call, quota


def edit_image(images_b64_json, prompt, style, token=None):
    """Run one edit. Returns (image_data_url, provider_call). Raises ProviderError (with the call) on failure."""
    t0 = time.monotonic()
    call = {"provider": PROVIDER, "route": f"{SPACE}{ENDPOINT}", "unit_type": "gpu_second", "succeeded": False}
    try:
        from gradio_client import Client  # imported lazily so billing and tests don't need it
        client = Client(SPACE, token=token or os.environ.get("HF_TOKEN") or None, verbose=False)
        out = client.predict(images_b64_json=images_b64_json, prompt=prompt, lora_adapter=style, seed=0,
                             randomize_seed=True, guidance_scale=1.0, steps=4, api_name=ENDPOINT)
        image = out.get("image") if isinstance(out, dict) else None
        if not image:
            raise RuntimeError("provider returned no image")
        call.update(succeeded=True, units=round(time.monotonic() - t0, 2),
                    detail={"style": style, "note": "units = wall-clock seconds (upper bound for GPU seconds)"})
        return image, call
    except Exception as e:  # noqa: BLE001 - any provider failure must release the reservation
        msg = str(e)
        quota = "quota" in msg.lower()
        # A quota refusal happens before any GPU is allocated, so it uses no GPU time
        call.update(units=0 if quota else round(time.monotonic() - t0, 2), detail={"error": msg[:300]})
        raise ProviderError(msg, call, quota=quota) from e
