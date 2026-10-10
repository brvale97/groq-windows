# Local speech detector

Silero VAD v6.2.3, from commit `5cd7945676eb32225748052e2e6a0580e4686a08`.
Source: https://raw.githubusercontent.com/snakers4/silero-vad/5cd7945676eb32225748052e2e6a0580e4686a08/src/silero_vad/data/silero_vad.onnx
Model SHA-256: `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`
Retrieved 2026-10-10. MIT license: `SILERO_LICENSE.txt`.

The model is shipped with the app and runs locally on the CPU through ONNX
Runtime. It needs no downloads, cloud calls, credentials, PyTorch or GPU.

Source of the streaming input/state/context contract:
https://github.com/snakers4/silero-vad/blob/5cd7945676eb32225748052e2e6a0580e4686a08/src/silero_vad/utils_vad.py
