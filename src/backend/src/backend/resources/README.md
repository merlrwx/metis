minilm-tokenizer.json is the unchanged tokenizer from
qdrant/all-MiniLM-L6-v2-onnx, revision
d13954661f83248295ba75c1ed411eef3b7b936e, for
sentence-transformers/all-MiniLM-L6-v2 (Apache License 2.0).

Source: https://huggingface.co/qdrant/all-MiniLM-L6-v2-onnx/blob/d13954661f83248295ba75c1ed411eef3b7b936e/tokenizer.json

Bundled so API/worker installations can count the pinned model's tokens offline
without downloading ONNX or installing its inference dependencies. The chunk
budget is 254 content tokens plus the model's two special tokens.
