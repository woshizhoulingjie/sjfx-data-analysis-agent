"""Small CPU OpenAI-compatible embedding server for SJFX."""

import os
from typing import List, Union
import threading
_admission = threading.BoundedSemaphore(1)

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer


MODEL_PATH = os.environ.get(
    "SJFX_EMBED_MODEL_PATH", "/home/zhoulingjie/models/multilingual-e5-small"
)
MODEL_NAME = os.environ.get("SJFX_EMBED_MODEL_NAME", "multilingual-e5-small")
MAX_LENGTH = int(os.environ.get("SJFX_EMBED_MAX_LENGTH", "512"))
MAX_BATCH = int(os.environ.get("SJFX_EMBED_MAX_BATCH", "8"))
THREADS = int(os.environ.get("OMP_NUM_THREADS", "1"))

torch.set_num_threads(max(1, THREADS))
torch.set_num_interop_threads(1)
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True)
model.eval()

app = FastAPI(title="SJFX Mail CPU Embeddings")


class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: str | None = None
    encoding_format: str | None = "float"


def mean_pool(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    return (last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_NAME, "device": "cpu"}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "sjfx"}]}


@app.post("/v1/embeddings")
def embeddings(request: EmbeddingRequest):
    if not _admission.acquire(timeout=5.0):
        raise HTTPException(status_code=503, detail="mail embedding service is busy", headers={"Retry-After": "1"})
    try:
        return _encode(request)
    finally:
        _admission.release()

def _encode(request: EmbeddingRequest):
    values = [request.input] if isinstance(request.input, str) else request.input
    if not values or len(values) > MAX_BATCH or any(not isinstance(x, str) for x in values):
        raise HTTPException(status_code=400, detail=f"input must contain 1-{MAX_BATCH} strings")
    encoded = tokenizer(
        values,
        padding=True,
        truncation=True,
        max_length=MAX_LENGTH,
        return_tensors="pt",
    )
    with torch.inference_mode():
        output = model(**encoded)
        vectors = torch.nn.functional.normalize(
            mean_pool(output.last_hidden_state, encoded["attention_mask"]), p=2, dim=1
        )
    token_count = int(encoded["attention_mask"].sum().item())
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vector.tolist(), "index": index}
            for index, vector in enumerate(vectors)
        ],
        "model": request.model or MODEL_NAME,
        "usage": {"prompt_tokens": token_count, "total_tokens": token_count},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "18083")))
